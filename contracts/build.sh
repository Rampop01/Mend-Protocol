#!/usr/bin/env bash
# Builds the escrow contract and copies the wasm to mend/contract/, where `mend setup` reads it.
set -euo pipefail
cd "$(dirname "$0")/bounty_escrow"
cargo build --target wasm32-unknown-unknown --release
out=../../mend/contract/mend_bounty_escrow.wasm
mkdir -p "$(dirname "$out")"
if command -v wasm-opt >/dev/null 2>&1; then
  wasm-opt -Oz --strip-debug target/wasm32-unknown-unknown/release/mend_bounty_escrow.wasm -o "$out"
else
  cp target/wasm32-unknown-unknown/release/mend_bounty_escrow.wasm "$out"
fi
echo "Wrote $out ($(wc -c < "$out") bytes)"
