// Client-side HPKE sealing for the payroll PII vault (Pillar C1).
//
// The browser seals SSN / bank / tax fields to an HSM-held recipient public key
// so the server never holds plaintext at rest. The output is the EXACT
// self-describing envelope the server validates and (in C2) opens — matching
// usali.pii_crypto.SealedEnvelope.to_json() field-for-field. The wire contract
// (suite label, `info`, AAD encoding, 65-byte SEC1 points) is pinned across both
// libraries by the committed interop fixture (tests/fixtures/hpke_interop.json);
// piiSeal.test.ts guards the JS side.

import { Aes256Gcm, CipherSuite, DhkemP256HkdfSha256, HkdfSha256 } from '@hpke/core'

// Byte-for-byte identical to usali.pii_crypto.SUITE_LABEL / _INFO and the
// pyhpke suite on the server. Changing either side silently breaks decryption.
const SUITE_LABEL = 'DHKEM-P256-HKDF-SHA256/HKDF-SHA256/AES-256-GCM'
const INFO = 'usali-payroll-pii/v1'
const ENVELOPE_VERSION = 1

/** The envelope JSON shape the server's SealedEnvelope.parse() consumes. */
export interface SealedEnvelopeJson {
  version: number
  suite: string
  key_id: string
  enc: string // base64 SEC1 uncompressed point (65 bytes)
  ct: string // base64 AEAD ciphertext
}

const utf8 = (s: string): Uint8Array => new TextEncoder().encode(s)

// @hpke/core's kem.importKey wants a true ArrayBuffer (not a typed-array view).
const b64ToArrayBuffer = (b64: string): ArrayBuffer => {
  const bin = atob(b64)
  const buf = new ArrayBuffer(bin.length)
  const view = new Uint8Array(buf)
  for (let i = 0; i < bin.length; i += 1) view[i] = bin.charCodeAt(i)
  return buf
}

const bytesToB64 = (u8: Uint8Array): string => btoa(String.fromCharCode(...u8))

/**
 * The aad slot for one deposit-account field — MUST mirror the backend's
 * `account_slot`/`routing_slot` derivation in src/usali/deposit_accounts.py
 * character for character: the server reconstructs this string at open time,
 * and a mismatch makes every deposit envelope fail to open at the first pay
 * run. The ordinal in the aad is what makes two accounts' ciphertexts
 * non-interchangeable.
 */
export function depositAad(
  employeeId: number,
  ordinal: number,
  field: 'account' | 'routing',
): string {
  return `${employeeId}:deposit:${ordinal}:${field}`
}

function newSuite(): CipherSuite {
  return new CipherSuite({
    kem: new DhkemP256HkdfSha256(),
    kdf: new HkdfSha256(),
    aead: new Aes256Gcm(),
  })
}

/**
 * Seal one PII field to the recipient public key, returning the envelope JSON.
 *
 * @param pubKeyB64  base64 SEC1 uncompressed P-256 point (from the pinned
 *                   /api/payroll/pii-public-key endpoint)
 * @param keyId      the recipient key id to stamp into the envelope (the server
 *                   rejects a write whose key_id is not the current key)
 * @param plaintext  the raw field value — sealed here, never sent in the clear
 * @param aad        `${employeeId}:${field}` — binds the ciphertext to its slot,
 *                   reconstructed deterministically by the server at open time
 */
export async function sealField(
  pubKeyB64: string,
  keyId: string,
  plaintext: string,
  aad: string,
): Promise<string> {
  const suite = newSuite()
  const recipientPublicKey = await suite.kem.importKey('raw', b64ToArrayBuffer(pubKeyB64), true)
  const sender = await suite.createSenderContext({
    recipientPublicKey,
    info: utf8(INFO),
  })
  const ct = await sender.seal(utf8(plaintext), utf8(aad))
  const envelope: SealedEnvelopeJson = {
    version: ENVELOPE_VERSION,
    suite: SUITE_LABEL,
    key_id: keyId,
    enc: bytesToB64(new Uint8Array(sender.enc)),
    ct: bytesToB64(new Uint8Array(ct)),
  }
  return JSON.stringify(envelope)
}
