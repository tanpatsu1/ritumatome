// 既読・重要の状態を端末間で同期する最小サーバー
// - GET  : 保存済みの状態(暗号文)を返す
// - PUT  : 状態(暗号文)を保存する
// 認証: Authorization: Bearer <SYNC_TOKEN>。中身は端末側でAES暗号化済みなので
//       サーバー(Cloudflare)には暗号文しか保存されない。
const KEY = "state";

function cors(extra) {
  return Object.assign({
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET,PUT,OPTIONS",
    "Access-Control-Allow-Headers": "Authorization,Content-Type",
  }, extra || {});
}

export default {
  async fetch(req, env) {
    if (req.method === "OPTIONS") {
      return new Response(null, { headers: cors() });
    }
    const auth = req.headers.get("Authorization") || "";
    if (auth !== "Bearer " + env.SYNC_TOKEN) {
      return new Response("unauthorized", { status: 401, headers: cors() });
    }
    if (req.method === "GET") {
      const v = await env.SYNC_KV.get(KEY);
      return new Response(v || "", { headers: cors({ "Content-Type": "text/plain" }) });
    }
    if (req.method === "PUT") {
      const body = await req.text();
      await env.SYNC_KV.put(KEY, body);
      return new Response("ok", { headers: cors() });
    }
    return new Response("method not allowed", { status: 405, headers: cors() });
  },
};
