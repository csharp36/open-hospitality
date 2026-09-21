/**
 * The webhook signature (D-OH23.2): HMAC-SHA256 over `timestamp + "\n" + body`.
 *
 * `crypto.subtle` is the only primitive available in both runtimes this file
 * must work in — the Workers runtime, where the worker runs, and Node 22,
 * where `node --test` runs it. Neither needs a dependency for it.
 *
 * The construction has a fixed test vector on both sides of the wire:
 * test/sign.test.js here, and tests/test_intake.py::
 * test_the_signature_vector_verifies in the app. Both assert the same hex for
 * secret "s", timestamp "1700000000", body "hello", which is what makes a
 * change to either side's construction fail a test rather than a night's mail.
 */

const encoder = new TextEncoder();

/**
 * @param {string} secret   the shared secret (USALI_EMAIL_INTAKE_SECRET)
 * @param {string} timestamp unix seconds, decimal, no sign or padding
 * @param {Uint8Array} bodyBytes the raw message, exactly as it will be POSTed
 * @returns {Promise<string>} the MAC as lowercase hex, with no `sha256=` prefix
 */
export async function sign(secret, timestamp, bodyBytes) {
  // A string here would be a silent catastrophe rather than an error: a
  // string has no `.length` in bytes that `Uint8Array.prototype.set` can use,
  // so `signed.set(bodyBytes, ...)` writes nothing and the body is MAC'd as a
  // run of zero bytes — every message would sign identically and the app
  // would refuse all of them. Fail loudly instead.
  // Pinned by test/sign.test.js's "a string body is refused, not signed as
  // zeros".
  if (!ArrayBuffer.isView(bodyBytes)) {
    throw new TypeError("sign() needs the body as a Uint8Array, not a string");
  }

  const key = await crypto.subtle.importKey(
    "raw",
    encoder.encode(secret),
    { name: "HMAC", hash: "SHA-256" },
    false,
    ["sign"],
  );

  // The separator is part of the signed bytes, not a formatting nicety: it is
  // what stops a timestamp/body pair being re-cut into a different pair with
  // the same MAC. test/sign.test.js's "the separator is signed: the timestamp
  // cannot be recut into the body" is the case that fails without it.
  const prefix = encoder.encode(`${timestamp}\n`);
  const signed = new Uint8Array(prefix.length + bodyBytes.length);
  signed.set(prefix, 0);
  signed.set(bodyBytes, prefix.length);

  const mac = await crypto.subtle.sign("HMAC", key, signed);
  return Array.from(new Uint8Array(mac), (byte) =>
    byte.toString(16).padStart(2, "0"),
  ).join("");
}
