#!/usr/bin/env bash
# Rebuilds mend/web/wallet.bundle.js (NEAR Wallet Selector v10 + Meteor, MyNearWallet, Intear) and wallet.css.
set -euo pipefail
cd "$(dirname "$0")"
npm install --no-save @near-wallet-selector/core@10.1.4 @near-wallet-selector/modal-ui-js@10.1.4 \
  @near-wallet-selector/meteor-wallet@10.1.4 @near-wallet-selector/my-near-wallet@10.1.4 \
  @near-wallet-selector/intear-wallet@10.1.4 esbuild buffer util process
npx esbuild entry.js --bundle --format=iife --global-name=MendWallet --platform=browser --target=es2020 \
  --minify --define:global=globalThis --inject:inject.js --outfile=../mend/web/wallet.bundle.js
cp node_modules/@near-wallet-selector/modal-ui-js/styles.css ../mend/web/wallet.css
