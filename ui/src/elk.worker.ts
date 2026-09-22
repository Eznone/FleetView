// ELK runs here and not on the main thread.
//
// §4 names dynamic-graph "spaghetti" as the biggest technical risk in this
// category of tool, and ELK is the named mitigation -- but a layered layout is
// real work, and doing it on the main thread is what turns §7's 100 ms node
// budget into a dropped frame at exactly the moment the fleet gets busy.
import ELK from "elkjs/lib/elk.bundled.js";

const elk = new ELK();

self.onmessage = async (message: MessageEvent) => {
  const { id, graph } = message.data;
  try {
    const laid = await elk.layout(graph);
    (self as unknown as Worker).postMessage({ id, laid });
  } catch (error) {
    (self as unknown as Worker).postMessage({ id, error: String(error) });
  }
};
