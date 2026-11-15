// Vale WhatsApp bridge: a small, free, open-source replacement for UAZAPI.
//
// It connects one WhatsApp number through Baileys (github.com/WhiskeySockets/Baileys, MIT) and
// speaks the same HTTP API and webhook format as Evolution API (github.com/EvolutionAPI/evolution-api),
// so Vale's "evolution" channel works with this bridge on a laptop or with a full Evolution API
// server later, without code changes.
//
//   POST /message/sendText/:instance              {number, text}
//   POST /message/sendWhatsAppAudio/:instance     {number, audio: base64 | data URI}
//   POST /chat/getBase64FromMediaMessage/:instance {message: {key: {id}}}  -> {base64, mimetype}
//   GET  /instance/connectionState/:instance      -> {instance: {state}}
//   GET  /qr                                       QR page to link the phone (localhost only)
//
// Incoming messages are POSTed to WEBHOOK_URL as {event: "messages.upsert", instance, data}.
// Every request needs the header  apikey: BRIDGE_API_KEY.

import http from "node:http";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import makeWASocket, {
  DisconnectReason,
  downloadMediaMessage,
  fetchLatestBaileysVersion,
  useMultiFileAuthState,
} from "baileys";
import pino from "pino";
import QRCode from "qrcode";

const here = path.dirname(fileURLToPath(import.meta.url));
loadEnv(path.join(here, ".env"));

const PORT = Number(process.env.BRIDGE_PORT || 8081);
const HOST = process.env.BRIDGE_HOST || "127.0.0.1";
const API_KEY = process.env.BRIDGE_API_KEY || "";
const INSTANCE = process.env.BRIDGE_INSTANCE || "vale";
const WEBHOOK_URL = process.env.WEBHOOK_URL || "";
const AUTH_DIR = path.resolve(here, process.env.AUTH_DIR || "auth");
const PAIR_PHONE = (process.env.PAIR_PHONE || "").replace(/\D/g, "");
const log = pino({ level: process.env.LOG_LEVEL || "warn" });

if (!API_KEY) {
  console.error("BRIDGE_API_KEY is empty: set it in bridge/whatsapp/.env");
  process.exit(1);
}

let sock = null;
let state = "connecting";
let lastQr = null;
let pairingCode = null;
const cache = new Map(); // message id -> WAMessage, for media download
const CACHE_MAX = 500;

function loadEnv(file) {
  if (!fs.existsSync(file)) return;
  for (const line of fs.readFileSync(file, "utf8").split(/\r?\n/)) {
    const m = line.match(/^\s*([A-Z0-9_]+)\s*=\s*(.*)\s*$/);
    if (m && process.env[m[1]] === undefined) process.env[m[1]] = m[2].replace(/^["']|["']$/g, "");
  }
}

function remember(msg) {
  cache.set(msg.key.id, msg);
  if (cache.size > CACHE_MAX) cache.delete(cache.keys().next().value);
}

function messageType(message) {
  if (!message) return "unknown";
  return Object.keys(message).find((k) => k !== "messageContextInfo" && k !== "senderKeyDistributionMessage") || "unknown";
}

async function postWebhook(body) {
  if (!WEBHOOK_URL) return;
  for (let attempt = 1; attempt <= 3; attempt++) {
    try {
      const r = await fetch(WEBHOOK_URL, {
        method: "POST",
        headers: { "content-type": "application/json", apikey: API_KEY },
        body: JSON.stringify(body),
        signal: AbortSignal.timeout(15000),
      });
      if (r.ok) return;
      console.warn(`webhook answered ${r.status} (attempt ${attempt})`);
    } catch (e) {
      console.warn(`webhook failed (attempt ${attempt}): ${e.message}`);
    }
    await new Promise((res) => setTimeout(res, attempt * 2000));
  }
}

async function start() {
  const { state: auth, saveCreds } = await useMultiFileAuthState(AUTH_DIR);
  const { version } = await fetchLatestBaileysVersion().catch(() => ({ version: undefined }));
  sock = makeWASocket({ auth, version, logger: log, browser: ["Vale", "Chrome", "1.0"], markOnlineOnConnect: false });
  sock.ev.on("creds.update", saveCreds);

  sock.ev.on("connection.update", async (u) => {
    if (u.qr) {
      lastQr = u.qr;
      state = "qr";
      if (PAIR_PHONE && !pairingCode && !sock.authState.creds.registered) {
        try {
          pairingCode = await sock.requestPairingCode(PAIR_PHONE);
          console.log(`\nPairing code: ${pairingCode}\nOn the phone: WhatsApp > Linked devices > Link with phone number.\n`);
        } catch (e) {
          console.warn(`pairing code failed: ${e.message}`);
        }
      }
      if (!PAIR_PHONE) console.log(`Scan the QR: open http://${HOST}:${PORT}/qr in the browser of this computer.`);
    }
    if (u.connection === "open") {
      state = "open";
      lastQr = null;
      pairingCode = null;
      console.log(`WhatsApp connected as ${sock.user?.id?.split(":")[0]}`);
    }
    if (u.connection === "close") {
      state = "close";
      const code = u.lastDisconnect?.error?.output?.statusCode;
      if (code === DisconnectReason.loggedOut) {
        console.error("Logged out on the phone. Delete the auth folder and start again to link a new session.");
        process.exit(2);
      }
      console.warn(`connection closed (${code ?? "unknown"}); reconnecting in 3 s`);
      setTimeout(start, 3000);
    }
  });

  sock.ev.on("messages.upsert", async ({ messages, type }) => {
    if (type !== "notify") return;
    for (const msg of messages) {
      const jid = msg.key?.remoteJid || "";
      if (!msg.message || msg.key.fromMe || jid.endsWith("@g.us") || jid.endsWith("@broadcast") || jid.endsWith("@newsletter")) continue;
      remember(msg);
      await postWebhook({
        event: "messages.upsert",
        instance: INSTANCE,
        data: {
          key: msg.key,
          pushName: msg.pushName || null,
          message: msg.message,
          messageType: messageType(msg.message),
          messageTimestamp: Number(msg.messageTimestamp || 0),
        },
      });
    }
  });
}

function toJid(number) {
  const s = String(number || "");
  if (s.includes("@")) return s;
  const digits = s.replace(/\D/g, "");
  if (!digits) throw Object.assign(new Error("number is required"), { status: 400 });
  return `${digits}@s.whatsapp.net`;
}

function readJson(req) {
  return new Promise((resolve, reject) => {
    let size = 0;
    const chunks = [];
    req.on("data", (c) => {
      size += c.length;
      if (size > 25 * 1024 * 1024) reject(Object.assign(new Error("body too large"), { status: 413 }));
      else chunks.push(c);
    });
    req.on("end", () => {
      try {
        resolve(chunks.length ? JSON.parse(Buffer.concat(chunks).toString("utf8")) : {});
      } catch {
        reject(Object.assign(new Error("invalid JSON"), { status: 400 }));
      }
    });
    req.on("error", reject);
  });
}

function send(res, status, body, type = "application/json") {
  res.writeHead(status, { "content-type": type });
  res.end(type === "application/json" ? JSON.stringify(body) : body);
}

function requireOpen() {
  if (state !== "open") throw Object.assign(new Error(`WhatsApp not connected (state: ${state})`), { status: 503 });
}

const routes = {
  "POST sendText": async (body) => {
    requireOpen();
    const sent = await sock.sendMessage(toJid(body.number), { text: String(body.text || "") });
    return { key: sent.key, status: "PENDING" };
  },
  "POST sendWhatsAppAudio": async (body) => {
    requireOpen();
    const b64 = String(body.audio || "").replace(/^data:[^,]+,/, "");
    const audio = Buffer.from(b64, "base64");
    const sent = await sock.sendMessage(toJid(body.number), { audio, ptt: true, mimetype: "audio/ogg; codecs=opus" });
    return { key: sent.key, status: "PENDING" };
  },
  "POST getBase64FromMediaMessage": async (body) => {
    const id = body?.message?.key?.id;
    const msg = id && cache.get(id);
    if (!msg) throw Object.assign(new Error("message not found (bridge restarted or too old)"), { status: 404 });
    const buffer = await downloadMediaMessage(msg, "buffer", {}, { logger: log, reuploadRequest: sock.updateMediaMessage });
    const inner = msg.message[messageType(msg.message)] || {};
    return { base64: buffer.toString("base64"), mimetype: inner.mimetype || "application/octet-stream" };
  },
  "GET connectionState": async () => ({ instance: { instanceName: INSTANCE, state } }),
};

const server = http.createServer(async (req, res) => {
  const url = new URL(req.url, `http://${req.headers.host || "localhost"}`);
  try {
    if (req.method === "GET" && url.pathname === "/health") return send(res, 200, { ok: true, state });
    if (req.method === "GET" && url.pathname === "/qr") {
      const local = ["127.0.0.1", "::1", "::ffff:127.0.0.1"].includes(req.socket.remoteAddress);
      if (!local) return send(res, 404, { error: "not found" });
      if (state === "open") return send(res, 200, "<h2>WhatsApp connected.</h2>", "text/html; charset=utf-8");
      if (!lastQr) return send(res, 200, "<meta http-equiv=refresh content=2><p>Waiting for the QR...</p>", "text/html; charset=utf-8");
      const img = await QRCode.toDataURL(lastQr, { width: 320, margin: 2 });
      return send(res, 200, `<meta http-equiv=refresh content=15><body style="font-family:sans-serif;text-align:center">
        <h2>Vale: link WhatsApp</h2><p>Phone: WhatsApp &gt; Linked devices &gt; Link a device</p><img src="${img}"></body>`,
        "text/html; charset=utf-8");
    }
    if (req.headers.apikey !== API_KEY) return send(res, 401, { error: "invalid apikey" });
    const [, group, action, instance] = url.pathname.split("/");
    const handler = routes[`${req.method} ${action}`];
    if (!group || !handler || (instance && instance !== INSTANCE)) return send(res, 404, { error: "not found" });
    const body = req.method === "POST" ? await readJson(req) : {};
    return send(res, 200, await handler(body));
  } catch (e) {
    const status = e.status || 500;
    if (status >= 500) console.error(e);
    return send(res, status, { error: e.message });
  }
});

server.listen(PORT, HOST, () => console.log(`Vale WhatsApp bridge on http://${HOST}:${PORT} (instance "${INSTANCE}")`));
start().catch((e) => {
  console.error(e);
  process.exit(1);
});
