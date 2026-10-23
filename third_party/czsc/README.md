# CZSC native dependency licenses and notices

This directory accompanies the two unmodified CZSC 1.0.1 native wheels identified in `index.json`. It supplements the original CZSC Apache-2.0 license and frozen source/build manifests; it does not replace or rewrite them.

The registry collection covers all 459 registry package versions in the frozen `Cargo.lock`. It is a conservative superset, not an assertion that every package was linked into either wheel. Each archive SHA-256 was checked against its Cargo.lock checksum before notice extraction. The index preserves the exact source URL, package licensing metadata, notice path and SHA-256.

Some crates omit full terms, ship a relative-license placeholder, or carry license text in `AUTHORS`. Supplements are identified per package: exact upstream commits come from the checksum-verified crate VCS metadata; the two winapi import-library crates omit VCS metadata, so their declared MIT/Apache terms are accompanied by the original same-project licenses from the separately locked winapi 0.3.9 package. No exact build commit is inferred for those two crates. Siphasher selects Apache-2.0 under its original COPYING grant. R-efi selects MIT; LGPL is an unselected alternative. The polars-parquet-format MIT AND Apache grant is accompanied by its original MIT notice, verbatim source licensing headers and full official Apache terms. No original notices are replaced.

`rust-toolchain/` includes the official Rust 1.98.1 and 1.95.0 rustc/rust-std notices, including `COPYRIGHT-library.html`, full compiler `COPYRIGHT.html`, LICENSE-MIT, LICENSE-APACHE and any component runtime notices. Complete component archives were verified against SHA-256 values in their official versioned release manifests. The recorded compiler versions and short commits match those release manifests. This is license provenance, not proof that the native wheels were rebuilt from these downloaded components. Full compiler copyright collections include more software than the standard library or either wheel necessarily uses.

All licenses and copyright statements remain under their original terms. The containing project's MIT license applies to its own code and does not relicense these components. Market data, user state and model weights are outside this source-code collection.

`SHA256SUMS` covers every file in this directory except itself. The index binds each notice back to its source. Archive downloads, collection scripts and host paths are excluded from this publication directory.
