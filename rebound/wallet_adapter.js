"use strict";
// EIP-1193 adapter for one Rebound wallet session. The review and approval happen
// in the Flet UI; this script only connects the wallet, hands it the transaction
// the server stored for this session, and reports the outcome. CONFIG is injected
// by rebound/wallet.py from rebound/protocol.py.
(() => {
  const status = document.getElementById("status");
  const say = (text) => { status.textContent = text; };
  const api = `/api/wallet-sessions/${CONFIG.session}`;
  const FIELDS = ["chainId", "data", "from", "gas", "maxFeePerGas", "maxPriorityFeePerGas", "to", "value"];
  const QUANTITY = /^0x(0|[1-9a-f][0-9a-f]*)$/;
  let provider = null;
  let account = null;
  let claimed = false;
  let stopped = false;

  async function call(path, body) {
    const options = { method: body === undefined ? "GET" : "POST", cache: "no-store", credentials: "omit" };
    if (body !== undefined) {
      options.headers = { "Content-Type": "application/json" };
      options.body = JSON.stringify(body);
    }
    const response = await fetch(api + path, options);
    const data = await response.json().catch(() => ({}));
    if (!response.ok) {
      const error = new Error(typeof data.detail === "string" ? data.detail : `Request failed (${response.status}).`);
      error.status = response.status;
      throw error;
    }
    return data;
  }

  const pause = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

  function waitForProvider() {
    if (window.ethereum) return Promise.resolve(window.ethereum);
    return new Promise((resolve) => {
      const done = () => resolve(window.ethereum || null);
      window.addEventListener("ethereum#initialized", done, { once: true });
      setTimeout(done, 3000);
    });
  }

  async function ensureArc() {
    let chain = String(await provider.request({ method: "eth_chainId" })).toLowerCase();
    if (chain === CONFIG.chainHex) return chain;
    try {
      await provider.request({ method: "wallet_switchEthereumChain", params: [{ chainId: CONFIG.chainHex }] });
    } catch (error) {
      if (!error || error.code !== 4902) throw error;
      await provider.request({
        method: "wallet_addEthereumChain",
        params: [{
          chainId: CONFIG.chainHex, chainName: "Arc Testnet", rpcUrls: [CONFIG.rpc],
          nativeCurrency: { name: "USDC", symbol: "USDC", decimals: 18 }, blockExplorerUrls: [CONFIG.explorer],
        }],
      });
    }
    chain = String(await provider.request({ method: "eth_chainId" })).toLowerCase();
    if (chain !== CONFIG.chainHex) throw new Error("The wallet is not on Arc Testnet.");
    return chain;
  }

  async function connect() {
    const accounts = await provider.request({ method: "eth_requestAccounts" });
    if (!Array.isArray(accounts) || !/^0x[0-9a-fA-F]{40}$/.test(accounts[0] || "")) {
      throw new Error("The wallet did not share an account.");
    }
    const chain = await ensureArc();
    account = accounts[0].toLowerCase();
    await call("/connected", { account, chainId: chain });
    say("Connected. Return to the Rebound tab to review and send.");
  }

  // The stored transaction must be exactly the reviewed shape, from this account,
  // to the fixed transmitter, on Arc, with zero value and explicit fee caps.
  function checked(tx) {
    if (!tx || typeof tx !== "object") throw new Error("missing transaction.");
    const keys = Object.keys(tx).sort();
    if (keys.length !== FIELDS.length || keys.some((key, index) => key !== FIELDS[index])) {
      throw new Error("unexpected transaction fields.");
    }
    if (tx.to !== CONFIG.transmitter) throw new Error("the contract is not Arc's MessageTransmitterV2.");
    if (tx.from !== account) throw new Error("the sender is not the connected account.");
    if (tx.chainId !== CONFIG.chainHex) throw new Error("the chain is not Arc Testnet.");
    if (tx.value !== "0x0") throw new Error("the transaction carries value.");
    if (typeof tx.data !== "string" || !tx.data.startsWith(CONFIG.selector) || !/^0x([0-9a-f]{2})+$/.test(tx.data)) {
      throw new Error("the calldata is not receiveMessage.");
    }
    for (const key of ["gas", "maxFeePerGas", "maxPriorityFeePerGas"]) {
      if (!QUANTITY.test(tx[key]) || (BigInt(tx[key]) === 0n && key !== "maxPriorityFeePerGas")) {
        throw new Error(`invalid ${key}.`);
      }
    }
    if (BigInt(tx.gas) > BigInt(CONFIG.maxGas)) throw new Error("the gas limit exceeds the recovery bound.");
    if (BigInt(tx.maxPriorityFeePerGas) > BigInt(tx.maxFeePerGas)) throw new Error("the priority fee exceeds the fee cap.");
    if (BigInt(tx.gas) * BigInt(tx.maxFeePerGas) > BigInt(CONFIG.maxNetworkFee)) {
      throw new Error("the maximum network fee exceeds the cap.");
    }
    const request = {};
    for (const key of FIELDS) request[key] = tx[key];
    return request;
  }

  async function report(body) {
    for (let attempt = 0; attempt < 5; attempt += 1) {
      try {
        await call("/result", body);
        return true;
      } catch (error) {
        if (error.status && error.status < 500) return false;
        await pause(1500);
      }
    }
    return false;
  }

  // Returns true only when the wallet rejected the request, so nothing was sent and
  // the user may approve again in Rebound. Every other path ends this page.
  async function send() {
    claimed = true;
    let claim;
    try {
      claim = await call("/claim", {});
    } catch (error) {
      claimed = false;  // The server did not hand over a transaction, so nothing can be sent.
      return true;
    }
    let request;
    try {
      request = checked(claim);
      const chain = String(await provider.request({ method: "eth_chainId" })).toLowerCase();
      const accounts = await provider.request({ method: "eth_accounts" });
      if (chain !== CONFIG.chainHex) throw new Error("the wallet left Arc Testnet.");
      if (!Array.isArray(accounts) || String(accounts[0] || "").toLowerCase() !== account) {
        throw new Error("the wallet's account changed.");
      }
    } catch (error) {
      await report({ outcome: "not_sent", reason: String(error.message || error).slice(0, 180) });
      say("Nothing was sent. Return to the Rebound tab.");
      return false;
    }
    say("Confirm the transaction in your wallet.");
    let hash;
    try {
      hash = await provider.request({ method: "eth_sendTransaction", params: [request] });
    } catch (error) {
      if (error && error.code === 4001 && await report({ outcome: "rejected" })) {
        say("You rejected the request. Nothing was sent. Return to the Rebound tab.");
        claimed = false;
        return true;
      }
      await report({ outcome: "unknown", code: Number.isInteger(error && error.code) ? error.code : null });
      say("The wallet did not confirm the outcome. Do not send again. Return to the Rebound tab.");
      return false;
    }
    if (typeof hash !== "string" || !/^0x[0-9a-fA-F]{64}$/.test(hash)) {
      await report({ outcome: "unknown", code: null });
      say("The wallet returned no transaction hash. Do not send again. Return to the Rebound tab.");
      return false;
    }
    const saved = await report({ outcome: "submitted", hash: hash.toLowerCase() });
    say(saved ? "Sent. Return to the Rebound tab to see the verified receipt."
      : `Sent ${hash}. Rebound did not receive the hash; paste it in the Rebound tab.`);
    return false;
  }

  async function watch() {
    while (!stopped) {
      let state;
      try {
        state = (await call("")).state;
      } catch (error) {
        if (error.status === 404) {
          say("This wallet session has ended. Return to the Rebound tab.");
          return;
        }
        await pause(2000);
        continue;
      }
      if (state === "approved" && !claimed) {
        if (!(await send())) return;
        continue;
      }
      if (claimed || ["confirmed", "failed", "submitted", "unknown", "unrelated"].includes(state)) return;
      await pause(1000);
    }
  }

  async function reconnect() {
    if (claimed || stopped) return;
    try {
      await connect();
    } catch (error) {
      say(`Wallet connection stopped: ${String(error.message || error).slice(0, 160)}`);
    }
  }

  async function start() {
    provider = await waitForProvider();
    if (!provider || typeof provider.request !== "function") {
      say("No browser wallet was found. Install or unlock one, then reload this tab.");
      return;
    }
    try {
      await connect();
    } catch (error) {
      say(error && error.code === 4001 ? "Connection was rejected in the wallet. Reload this tab to try again."
        : `Wallet connection stopped: ${String(error.message || error).slice(0, 160)}`);
      return;
    }
    if (typeof provider.on === "function") {
      provider.on("accountsChanged", reconnect);
      provider.on("chainChanged", reconnect);
    }
    await watch();
    stopped = true;
  }

  start();
})();
