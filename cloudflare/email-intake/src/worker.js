/**
 * The Cloudflare Email Worker for emailed night audits (OH-23, D-OH23.1).
 *
 * Every message sent to a `*@intake.<domain>` address arrives here through the
 * catch-all Email Routing rule. The worker does nothing clever: it reads the
 * raw bytes, signs them, and POSTs them to the app as `message/rfc822` with
 * five headers. It does not parse MIME, does not look at attachments, and
 * never logs message content — the app is where a message is opened, behind
 * the redaction gate.
 *
 * TWO DISPOSITIONS AND ONLY TWO. `deliver` below answers true when the app
 * accepted the message and false otherwise, and this handler forwards to the
 * fallback mailbox on false. There is no third branch: `setReject` is not
 * called anywhere in this file, because a bounce to a PMS's automated sender
 * is silent — nobody reads it and the night's report is gone.
 *
 * WHY A NON-2xx IS A FORWARD. `src/usali/intake_api.py` is where the status
 * codes are chosen, and it reserves non-2xx for "this service did not dispose
 * of the message" (401 bad signature or stale timestamp, 413 over the cap, 429
 * flood, 5xx unhandled). Every disposition the app DID make — including
 * `unknown_address`, `sender_rejected` and `failed` — is a 200 carrying an
 * outcome. So a non-2xx here means the message still needs a home, and the
 * fallback mailbox is it.
 *
 * WHAT THE FALLBACK MAILBOX HOLDS. Unredacted night-audit reports, outside the
 * app's redaction gate. It is an ops mailbox; docs/runbooks/email-intake.md is
 * where that is spelled out for whoever configures it.
 */

import { sign } from "./sign.js";

/** What the app expects the body to be labeled as. */
const CONTENT_TYPE = "message/rfc822";

/**
 * Read a stream into one Uint8Array, giving up once the running total passes
 * `maxBytes`.
 *
 * Returns null rather than the bytes when the cap is passed, and cancels the
 * stream at that point, so at most the cap plus one chunk is ever held. The
 * app refuses an over-large body with 413 anyway; stopping here is what keeps
 * the worker's own memory bounded, since an Email Worker has no way to stream
 * a body it must also sign.
 *
 * @param {ReadableStream<Uint8Array>} stream
 * @param {number} maxBytes
 * @returns {Promise<Uint8Array|null>}
 */
async function readCapped(stream, maxBytes) {
  const reader = stream.getReader();
  const chunks = [];
  let total = 0;

  for (;;) {
    const { done, value } = await reader.read();
    if (done) {
      break;
    }
    chunks.push(value);
    total += value.byteLength;
    if (total > maxBytes) {
      await reader.cancel();
      return null;
    }
  }

  const body = new Uint8Array(total);
  let at = 0;
  for (const chunk of chunks) {
    body.set(chunk, at);
    at += chunk.byteLength;
  }
  return body;
}

/**
 * Log what the app said it did with the message, and nothing else.
 *
 * The response is parsed as JSON and re-serialized rather than logged as
 * text. That buys one specific property and no more: whatever is logged is
 * valid JSON with its control characters escaped, so a response cannot inject
 * newlines and forge extra log lines. It does NOT bound the length, and it
 * does not vouch for the content — a response that parsed is logged whatever
 * it says. What keeps report content out of it is the app: `intake_api._entry`
 * and `_stored` are where every string in that payload is masked.
 *
 * Never throws: the message has already been accepted by the time this runs,
 * and a logging failure is not a reason to mail it to a human.
 *
 * @param {Response} response
 */
async function logOutcome(response) {
  try {
    console.log(JSON.stringify(await response.json()));
  } catch {
    console.error("intake accepted the message but answered unreadable JSON");
  }
}

/**
 * POST the message to the app.
 *
 * @returns {Promise<boolean>} true only when the app answered 2xx, i.e. only
 *   when it has taken responsibility for the message. Every other path —
 *   over the cap, non-2xx, a fetch that threw — answers false and is the
 *   caller's cue to forward.
 */
async function deliver(message, env) {
  const maxBytes = Number(env.MAX_BYTES);

  // A missing or unparseable MAX_BYTES is a misconfiguration, and the safe
  // reading of one is the strict one. Without this, `Number(undefined)` is
  // NaN, every `> NaN` comparison below is false, and the cap silently stops
  // existing — the worker would read a message of any size into memory and
  // POST it. Forwarding instead puts the message somewhere a human will see
  // it, which is what a worker that cannot trust its own config should do.
  if (!Number.isFinite(maxBytes)) {
    console.error("MAX_BYTES is not a finite number — refusing to POST");
    return false;
  }

  // `rawSize` is Cloudflare's own count of the message bytes; when it is
  // present an over-large message costs no read at all.
  if (typeof message.rawSize === "number" && message.rawSize > maxBytes) {
    console.error(`message over MAX_BYTES (${message.rawSize} bytes)`);
    return false;
  }

  try {
    const body = await readCapped(message.raw, maxBytes);
    if (body === null) {
      console.error("message over MAX_BYTES");
      return false;
    }

    // Unix seconds, decimal. `usali.intake` refuses a timestamp of more than
    // 12 digits, which this stays under until the year 33658.
    const timestamp = Math.floor(Date.now() / 1000).toString();
    const signature = await sign(env.INTAKE_SECRET, timestamp, body);

    const response = await fetch(env.INTAKE_URL, {
      method: "POST",
      headers: {
        "Content-Type": CONTENT_TYPE,
        "X-Intake-Timestamp": timestamp,
        "X-Intake-Signature": `sha256=${signature}`,
        // ENVELOPE values, not the MIME `To:`/`From:`: the MIME headers are
        // author-controlled, and these two decide which property the message
        // lands on and whether its sender is allowed (D-OH23.1).
        "X-Intake-To": message.to,
        "X-Intake-From": message.from,
        // Cloudflare's own SPF/DKIM/DMARC verdicts.
        //
        // The `?? ""` is not about the app's default — the app already reads
        // an absent header as "" and `intake_api.receive_email` passes
        // `sender_allowed(auth_result or None, ...)`, so absent and empty
        // reach the policy identically, and tests/test_intake.py::
        // test_a_missing_authentication_results_header_is_refused pins that
        // a header with nothing in it vouches for nobody.
        //
        // It is about THIS side. `Headers.get` answers null for a header the
        // message does not carry, and a null in a header init is stringified
        // by the fetch spec: the app would receive the four characters
        // `null`, a non-empty free-text header handed to a parser. Verified
        // in Node 22: `new Headers({h: null}).get("h") === "null"`.
        "X-Intake-Auth": message.headers.get("authentication-results") ?? "",
      },
      body,
      // NEVER follow a redirect. The default, "follow", would re-POST the
      // whole raw message AND its valid signature to whatever host a 307
      // names — an unredacted night audit sent off-origin, and a 2xx from
      // that host would read here as "the app disposed of it", so the
      // message would not even reach the fallback mailbox. With "manual" a
      // 3xx arrives as a response whose `.ok` is false, which is already the
      // forward path below.
      redirect: "manual",
    });

    if (!response.ok) {
      // The status only. The response body of a refusal is the app's, and
      // this worker's logs are not a place to reproduce anything that came
      // out of a message.
      console.error(`intake refused the message: HTTP ${response.status}`);
      return false;
    }
    await logOutcome(response);
    return true;
  } catch (error) {
    console.error(`intake POST did not complete: ${error?.name ?? "Error"}`);
    return false;
  }
}

export default {
  async email(message, env, ctx) {
    if (!(await deliver(message, env))) {
      await message.forward(env.FALLBACK_ADDRESS);
    }
  },
};
