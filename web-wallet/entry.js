import { Buffer } from "buffer";
globalThis.Buffer = globalThis.Buffer || Buffer;
import { setupWalletSelector, actionCreators } from "@near-wallet-selector/core";
import { setupModal } from "@near-wallet-selector/modal-ui-js";
import { setupMeteorWallet } from "@near-wallet-selector/meteor-wallet";
import { setupMyNearWallet } from "@near-wallet-selector/my-near-wallet";
import { setupIntearWallet } from "@near-wallet-selector/intear-wallet";

export async function connect(network, contractId) {
  const selector = await setupWalletSelector({
    network,
    modules: [setupMeteorWallet(), setupMyNearWallet(), setupIntearWallet()],
  });
  const modal = setupModal(selector, { contractId, description: "Connect a NEAR wallet to Mend Protocol." });
  return { selector, modal };
}
export { actionCreators };
