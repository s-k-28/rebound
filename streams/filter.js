// QuickNode Streams infrastructure filter. The Rebound application is Python.
// Dataset: Base Sepolia, block_with_receipts. See docs/QUICKNODE.md.
function main(stream) {
  const messenger = "0x8fe6b999dc680ccfdd5bf7eb0974218be2542daa";
  const burnTopic = "0x0c8c1cbdc5190613ebd485511d4e2812cfa45eecb79d845893331fedad5130a5";
  const usdcTopic = "0x000000000000000000000000036cbd53842c5426634e7929541ec2318f3dcf7e";
  const logs = [];

  for (const entry of stream.data || []) {
    for (const receipt of entry.receipts || []) {
      if (receipt.status !== "0x1") continue;
      for (const log of receipt.logs || []) {
        const topics = log.topics || [];
        if (String(log.address).toLowerCase() !== messenger || topics.length !== 4) continue;
        if (String(topics[0]).toLowerCase() !== burnTopic) continue;
        if (String(topics[1]).toLowerCase() !== usdcTopic) continue;
        logs.push({
          address: log.address,
          topics: log.topics,
          data: log.data,
          transactionHash: log.transactionHash || receipt.transactionHash,
          blockNumber: log.blockNumber || receipt.blockNumber,
          blockHash: log.blockHash || receipt.blockHash,
          logIndex: log.logIndex,
          removed: Boolean(log.removed),
        });
      }
    }
  }
  return logs.length ? { logs } : null;
}
