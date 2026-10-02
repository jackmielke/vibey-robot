// vibey-telegram-webhook: Telegram's webhook for @Vibey_Robot.
//
// Every update lands in public.vibey_telegram_inbox (idempotent on update_id)
// and the Mac's reachy_telegram.py drains it. If the Mac hasn't touched
// public.vibey_heartbeat in HEARTBEAT_STALE_S, the sender gets one "not awake"
// note: once per DM chat per hour, and in groups only when Vibey is addressed.
//
// Secrets: TELEGRAM_VIBEY_TOKEN, TELEGRAM_WEBHOOK_SECRET.
// SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY are injected by the platform.

const TOKEN = Deno.env.get("TELEGRAM_VIBEY_TOKEN") ?? "";
const SECRET = Deno.env.get("TELEGRAM_WEBHOOK_SECRET") ?? "";
const SB_URL = Deno.env.get("SUPABASE_URL") ?? "";
const SB_KEY = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY") ?? "";
const BOT_HANDLE = (Deno.env.get("TELEGRAM_VIBEY_HANDLE") ?? "vibey_robot").toLowerCase();

const HEARTBEAT_STALE_S = 120;
const OFFLINE_COOLDOWN_S = 3600;
const OFFLINE_TEXT = "i'm not awake right now 😴 but i'll get back to you when i am";

const sbHeaders = {
  apikey: SB_KEY,
  Authorization: `Bearer ${SB_KEY}`,
  "Content-Type": "application/json",
};

function safeEqual(a: string, b: string): boolean {
  if (!a || a.length !== b.length) return false;
  let diff = 0;
  for (let i = 0; i < a.length; i++) diff |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return diff === 0;
}

async function rest(path: string, init: RequestInit = {}) {
  const r = await fetch(`${SB_URL}/rest/v1/${path}`, {
    ...init,
    headers: { ...sbHeaders, ...(init.headers ?? {}) },
  });
  if (!r.ok) throw new Error(`rest ${path.split("?")[0]} ${r.status}: ${await r.text()}`);
  const t = await r.text();
  return t ? JSON.parse(t) : null;
}

// deno-lint-ignore no-explicit-any
function addressed(msg: any): boolean {
  const low = String(msg.text ?? "").toLowerCase();
  if (low.includes(`@${BOT_HANDLE}`) || /\bvibey\b/.test(low)) return true;
  const from = msg.reply_to_message?.from ?? {};
  return Boolean(from.is_bot && String(from.username ?? "").toLowerCase() === BOT_HANDLE);
}

// deno-lint-ignore no-explicit-any
async function maybeOfflineReply(update: any): Promise<void> {
  const msg = update.message; // edits never trigger it
  if (!msg || typeof msg.text !== "string") return;
  const chatId = msg.chat?.id;
  const type = msg.chat?.type;
  if (type !== "private" && !(["group", "supergroup"].includes(type) && addressed(msg))) return;

  const hb = await rest("vibey_heartbeat?id=eq.bot&select=last_seen");
  const last = hb?.[0]?.last_seen ? Date.parse(hb[0].last_seen) : 0;
  if (Date.now() - last < HEARTBEAT_STALE_S * 1000) return; // Mac is up

  const since = new Date(Date.now() - OFFLINE_COOLDOWN_S * 1000).toISOString();
  const recent = await rest(
    `vibey_telegram_inbox?chat_id=eq.${chatId}&offline_replied_at=gte.${since}&select=update_id&limit=1`,
  );
  if (recent?.length) return;

  const body: Record<string, unknown> = { chat_id: chatId, text: OFFLINE_TEXT };
  if (type !== "private") body.reply_to_message_id = msg.message_id;
  const r = await fetch(`https://api.telegram.org/bot${TOKEN}/sendMessage`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!r.ok) {
    console.error(`sendMessage ${r.status}`);
    return;
  }
  await rest(`vibey_telegram_inbox?update_id=eq.${update.update_id}`, {
    method: "PATCH",
    body: JSON.stringify({ offline_replied_at: new Date().toISOString() }),
  });
}

Deno.serve(async (req) => {
  if (req.method !== "POST") return new Response("ok");
  const got = req.headers.get("x-telegram-bot-api-secret-token") ?? "";
  if (!SECRET || !safeEqual(got, SECRET)) return new Response("forbidden", { status: 403 });

  // deno-lint-ignore no-explicit-any
  let update: any;
  try {
    update = await req.json();
  } catch {
    return new Response("bad json", { status: 400 });
  }
  if (typeof update?.update_id !== "number") return new Response("ok");

  const msg = update.message ?? update.edited_message;
  let inserted: unknown[] = [];
  try {
    inserted = await rest("vibey_telegram_inbox?on_conflict=update_id", {
      method: "POST",
      headers: { Prefer: "resolution=ignore-duplicates,return=representation" },
      body: JSON.stringify({
        update_id: update.update_id,
        chat_id: msg?.chat?.id ?? null,
        payload: update,
      }),
    }) ?? [];
  } catch (e) {
    console.error(String(e));
    // 500 makes Telegram retry later, which is what we want if the DB blipped.
    return new Response("store failed", { status: 500 });
  }
  if (!inserted.length) return new Response("dup"); // redelivery, already handled

  const work = maybeOfflineReply(update).catch((e) => console.error(String(e)));
  // deno-lint-ignore no-explicit-any
  const rt = (globalThis as any).EdgeRuntime;
  if (rt?.waitUntil) rt.waitUntil(work);
  else await work;
  return new Response("ok");
});
