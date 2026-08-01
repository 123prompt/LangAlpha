// Wrapped in an IIFE so the captured host functions stay in closure scope: the
// runtime rewrites top-level `const` to `var`, which would republish them as
// global properties and undo the delete below.
(function () {
  const hostAgent = __host_agent;
  const hostPhase = __host_phase;
  const hostLog = __host_log;
  // The host bindings take raw arguments and skip the argument checks these
  // wrappers perform, so a script must not be able to reach them directly.
  delete globalThis.__host_agent;
  delete globalThis.__host_phase;
  delete globalThis.__host_log;

  globalThis.agent = async function agent(prompt, opts) {
    if (typeof prompt !== "string" || prompt.length === 0)
      throw new TypeError("agent(prompt, opts): prompt must be a non-empty string");
    if (opts !== undefined && (typeof opts !== "object" || opts === null || Array.isArray(opts)))
      throw new TypeError("agent(prompt, opts): opts must be a plain object");
    const response = await hostAgent(prompt, opts ?? {});
    if (!response.ok) throw new Error(response.error);
    return response.value;
  };

  globalThis.phase = function phase(title) { hostPhase(String(title)); };
  globalThis.log = function log(message) { hostLog(String(message)); };

  globalThis.parallel = async function parallel(thunks) {
    if (!Array.isArray(thunks)) throw new TypeError("parallel(thunks): thunks must be an array of functions");
    const settled = await Promise.allSettled(thunks.map((t) => Promise.resolve().then(() => t())));
    return settled.map((s) => (s.status === "fulfilled" ? s.value : null));
  };

  globalThis.pipeline = async function pipeline(items, ...stages) {
    if (!Array.isArray(items)) throw new TypeError("pipeline(items, ...stages): items must be an array");
    return Promise.all(items.map(async (item, index) => {
      let prev = item;
      for (const stage of stages) {
        try { prev = await stage(prev, item, index); }
        catch (e) { return null; }
      }
      return prev;
    }));
  };

  for (const k of ["agent", "phase", "log", "parallel", "pipeline"]) {
    Object.defineProperty(globalThis, k, { writable: false, configurable: false });
  }
})();
