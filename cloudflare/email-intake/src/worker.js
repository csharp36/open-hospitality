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
 * The response is re-serialized from parsed JSON rather than logged as text,
 * so a response that is not the documented `{outcome, attachments}` object
 * cannot put arbitrary bytes in the log line. Never throws: the message has
 * already been accepted by the time this runs, and a logging failure is not a
 * reason to mail it to a human.
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
        // Cloudflare's own SPF/DKIM/DMARC verdicts. Sent as "" when the
        // header is absent rather than omitted, so the app sees the same
        // empty string either way; tests/test_intake.py::
        // test_a_missing_authentication_results_header_is_refused is what
        // holds an empty header to a refusal there.
        "X-Intake-Auth": message.headers.get("authentication-results") ?? "",
      },
      body,
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
