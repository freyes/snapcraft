# -*- Mode:Python; indent-tabs-mode:nil; tab-width:4 -*-
#
# Copyright (C) 2016-2017 Marius Gripsgard (mariogrip@ubuntu.com)
# Copyright (C) 2016-2019 Canonical Ltd
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License version 3 as
# published by the Free Software Foundation.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

"""This rust plugin is useful for building rust based parts.

Rust uses cargo to drive the build.

This plugin uses the common plugin keywords as well as those for "sources".
For more information check the 'plugins' topic for the former and the
'sources' topic for the latter.

Additionally, this plugin uses the following plugin-specific keywords:

    - rust-channel
      (string)
      select rust channel (stable, beta, nightly)
    - rust-revision
      (string)
      select rust version, this is preferred over rust-channel
    - rust-features
      (list of strings)
      Features used to build optional dependencies

If a rust-toolchain file is found, the toolchain specified there will
be used unless rust-channel or rust-revision are set which will
override the rust-toolchain file. If neither rust-toolchain exists nor
rust-channel or rust-revision are set, the latest stable toolchain
will be used.
"""

import collections
import logging
import os
from contextlib import suppress
from typing import List, Optional

import toml

import snapcraft
from snapcraft import sources
from snapcraft import shell_utils
from snapcraft.internal import errors

_RUSTUP = "https://sh.rustup.rs/"
logger = logging.getLogger(__name__)


class RustPlugin(snapcraft.BasePlugin):
    @classmethod
    def schema(cls):
        schema = super().schema()
        schema["properties"]["rust-channel"] = {
            "type": "string",
            "enum": ["stable", "beta", "nightly"],
        }
        schema["properties"]["rust-revision"] = {"type": "string"}
        schema["properties"]["rust-features"] = {
            "type": "array",
            "minitems": 1,
            "uniqueItems": True,
            "items": {"type": "string"},
            "default": [],
        }
        schema["required"] = ["source"]

        return schema

    @classmethod
    def get_pull_properties(cls):
        return ["rust-revision", "rust-channel"]

    @classmethod
    def get_build_properties(cls):
        return ["rust-features"]

    def __init__(self, name, options, project):
        super().__init__(name, options, project)

        if project.info.base not in ("core", "core16", "core18"):
            raise errors.PluginBaseError(part_name=self.name, base=project.info.base)

        self.build_packages.extend(["gcc", "git", "curl", "file"])
        self._rust_dir = os.path.expanduser(os.path.join("~", ".cargo"))
        self._rustup_cmd = os.path.join(self._rust_dir, "bin", "rustup")
        self._cargo_cmd = os.path.join(self._rust_dir, "bin", "cargo")
        self._rustc_cmd = os.path.join(self._rust_dir, "bin", "rustc")
        self._rustdoc_cmd = os.path.join(self._rust_dir, "bin", "rustdoc")

        self._manifest = collections.OrderedDict()

    def enable_cross_compilation(self):
        # The logic is applied transparently trough internal
        # rust tooling.
        pass

    def pull(self):
        super().pull()
        self._fetch_rustup()
        self._fetch_rust()
        self._fetch_cargo_deps()

    def _fetch_rustup(self):
        # if rustup-init has already been done, we can skip this.
        if os.path.exists(os.path.join(self._rust_dir, "bin", "rustup")):
            return

        # Download rustup-init.
        os.makedirs(self._rust_dir, exist_ok=True)
        rustup_init_cmd = os.path.join(self._rust_dir, "rustup.sh")
        sources.Script(_RUSTUP, self._rust_dir).download(filepath=rustup_init_cmd)

        # Basic options:
        # -y: assume yes
        # --no-modify-path: do not modify bashrc
        options = ["-y", "--no-modify-path"]
        if self._get_toolchain() is not None:
            options.extend(["--default-toolchain", "none"])

        # Fetch rust
        self.run([rustup_init_cmd] + options, env=self._build_env())

    def _fetch_rust(self) -> None:
        toolchain = self._get_toolchain()

        # We only do this when one of the snapcraft properties are set,
        # the reason for this is faster iterations when changing any
        # of the toolchain related properties.
        # When changing the rust-toolchain file a re-pull will be triggered
        # due to source changes and the right thing will happen.
        if toolchain is not None:
            # https://rust-lang-nursery.github.io/edition-guide/rust-2018/rustup-for-managing-rust-versions.html
            self.run([self._rustup_cmd, "install", toolchain], env=self._build_env())

        # Add the appropriate target cross compilation target if necessary.
        # https://github.com/rust-lang/rustup.rs/blob/master/README.md#cross-compilation
        if self.project.is_cross_compiling:
            add_target_cmd = [self._rustup_cmd, "target", "add"]
            if toolchain is not None:
                add_target_cmd.extend(["--toolchain", toolchain])
            add_target_cmd.append(self._get_target())

            self.run(add_target_cmd, env=self._build_env())

    def _fetch_cargo_deps(self):
        if self.options.source_subdir:
            sourcedir = os.path.join(self.sourcedir, self.options.source_subdir)
        else:
            sourcedir = self.sourcedir

        fetch_cmd = [
            self._cargo_cmd,
            "fetch",
            "--manifest-path",
            os.path.join(sourcedir, "Cargo.toml"),
        ]
        toolchain = self._get_toolchain()
        if toolchain is not None:
            fetch_cmd.insert(1, "+{}".format(toolchain))
        self.run(fetch_cmd, env=self._build_env())

    def _get_target(self) -> str:
        # Cf. rustc --print target-list
        targets = {
            "armhf": "armv7-{}-{}eabihf",
            "arm64": "aarch64-{}-{}",
            "i386": "i686-{}-{}",
            "amd64": "x86_64-{}-{}",
            "ppc64el": "powerpc64le-{}-{}",
        }
        rust_target = targets.get(self.project.deb_arch)
        if not rust_target:
            raise errors.SnapcraftEnvironmentError(
                "{!r} is not supported as a target architecture when "
                "cross-compiling with the rust plugin".format(self.project.deb_arch)
            )
        return rust_target.format("unknown-linux", "gnu")

    def build(self):
        super().build()

        # Write a minimal config.
        self._write_cargo_config()

        install_cmd = [
            self._cargo_cmd,
            "install",
            "--path",
            self.builddir,
            "--root",
            self.installdir,
            "--force",
        ]
        toolchain = self._get_toolchain()
        if toolchain is not None:
            install_cmd.insert(1, "+{}".format(toolchain))

        # Even though this is mostly harmless when not cross compiling
        # the flag is in place to avoid a situation where an earlier
        # version of the toolchain is used.
        if self.project.is_cross_compiling:
            install_cmd.extend(["--target", self._get_target()])

        if self.options.rust_features:
            install_cmd.append("--features")
            install_cmd.append(" ".join(self.options.rust_features))

        # build and install.
        self.run(install_cmd, env=self._build_env())

        # Finally, record.
        self._record_manifest()

    def _build_env(self):
        env = os.environ.copy()

        env.update(dict(RUSTUP_HOME=self._rust_dir, CARGO_HOME=self._rust_dir))

        rustflags = self._get_rustflags()
        if rustflags:
            string_fmt = " ".join(["{}".format(i) for i in rustflags]).strip()
            env.update(RUSTFLAGS=string_fmt)

        return env

    def _get_toolchain(self) -> Optional[str]:
        """Return the toolchain to use.

        If a rust-toolchain file is present, None will be returned.
        If a rust-toolchain file is not present and neither rust-version
        nor rust-channel are set, then stable will be returned.
        """
        if self.options.source_subdir:
            sourcedir = os.path.join(self.sourcedir, self.options.source_subdir)
        else:
            sourcedir = self.sourcedir
        rust_toolchain_path = os.path.join(sourcedir, "rust-toolchain")

        if self.options.rust_revision:
            toolchain = self.options.rust_revision
        elif self.options.rust_channel:
            toolchain = self.options.rust_channel
        elif not os.path.exists(rust_toolchain_path):
            toolchain = "stable"
        else:
            toolchain = None

        return toolchain

    def _get_linker(self) -> str:
        arch_triplet = self.project.arch_triplet
        if arch_triplet == "i386-linux-gnu":
            return "i686-linux-gnu-gcc"
        else:
            return "{}-gcc".format(arch_triplet)

    def _write_cargo_config(self, cargo_config_path: Optional[str] = None) -> None:
        if cargo_config_path is None:
            cargo_config_path = os.path.join(self.builddir, ".cargo", "config")
        if os.path.dirname(cargo_config_path):
            os.makedirs(os.path.dirname(cargo_config_path), exist_ok=True)

        target = {self._get_target(): dict(linker=self._get_linker())}
        config = dict(
            arch_triplet=self.project.arch_triplet,
            jobs=self.parallel_build_count,
            rustc_cmd=self._rustc_cmd,
            rustdoc_cmd=self._rustdoc_cmd,
            target=target,
        )

        # Cf. http://doc.crates.io/config.html
        with open(cargo_config_path, "w") as toml_config_file:
            toml.dump(config, toml_config_file)

    def _get_rustflags(self) -> List[str]:
        ldflags = shell_utils.getenv("LDFLAGS")
        rustldflags = []
        flags = {flag for flag in ldflags.split(" ") if flag}
        for flag in flags:
            rustldflags.extend(["-C", "link-arg={}".format(flag)])

        if self.project.is_cross_compiling:
            rustldflags.extend(["-C", "linker={}".format(self._get_linker())])

        return rustldflags

    def _record_manifest(self):
        self._manifest["rustup-version"] = self.run_output(
            [self._rustup_cmd, "--version"], env=self._build_env()
        )
        toolchain = self._get_toolchain()
        if toolchain is not None:
            toolchain_option = "+{}".format(toolchain)
            self._manifest["rustc-version"] = self.run_output(
                [self._rustc_cmd, toolchain_option, "--version"], env=self._build_env()
            )
            self._manifest["cargo-version"] = self.run_output(
                [self._cargo_cmd, toolchain_option, "--version"], env=self._build_env()
            )
        else:
            self._manifest["rustc-version"] = self.run_output(
                [self._rustc_cmd, "--version"], env=self._build_env()
            )
            self._manifest["cargo-version"] = self.run_output(
                [self._cargo_cmd, "--version"], env=self._build_env()
            )
        with suppress(FileNotFoundError, IsADirectoryError):
            with open(os.path.join(self.builddir, "Cargo.lock")) as lock_file:
                self._manifest["cargo-lock-contents"] = lock_file.read()

    def get_manifest(self):
        return self._manifest
