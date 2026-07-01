/**
 * DPoP (RFC 9449) keypair persistence — shared between /auth/callback and the
 * mod panel (/).
 *
 * The keypair is generated on the callback page (right before it submits the
 * public JWK to /auth/token, which binds the thumbprint into the issued JWT
 * as cnf.jkt).  The callback page then does a full navigation back to '/',
 * which destroys all JS memory — sessionStorage can't carry a non-extractable
 * CryptoKey across that.  IndexedDB can: browsers support structured-cloning
 * CryptoKey objects into it, so the private key never has to be exported.
 * The mod panel loads the same keypair back out on the other side of the
 * navigation, so it matches the jkt already baked into the token.
 */

const DPOP_DB_NAME  = 'musichat-dpop';
const DPOP_STORE     = 'keys';
const DPOP_RECORD_ID = 'current';

function _dpopOpenDb() {
  return new Promise((resolve, reject) => {
    const req = indexedDB.open(DPOP_DB_NAME, 1);
    req.onupgradeneeded = () => {
      req.result.createObjectStore(DPOP_STORE);
    };
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => reject(req.error);
  });
}

async function dpopSaveKeyPair(keyPair) {
  const db = await _dpopOpenDb();
  return new Promise((resolve, reject) => {
    const tx = db.transaction(DPOP_STORE, 'readwrite');
    tx.objectStore(DPOP_STORE).put(keyPair, DPOP_RECORD_ID);
    tx.oncomplete = () => resolve();
    tx.onerror = () => reject(tx.error);
  });
}

async function dpopLoadKeyPair() {
  const db = await _dpopOpenDb();
  return new Promise((resolve, reject) => {
    const tx = db.transaction(DPOP_STORE, 'readonly');
    const req = tx.objectStore(DPOP_STORE).get(DPOP_RECORD_ID);
    req.onsuccess = () => resolve(req.result || null);
    req.onerror = () => reject(req.error);
  });
}

async function dpopExportPublicJwk(keyPair) {
  const jwk = await crypto.subtle.exportKey('jwk', keyPair.publicKey);
  delete jwk.d;          // strip private component if somehow present
  delete jwk.key_ops;    // strip usage hints (not needed for verification)
  delete jwk.ext;
  return jwk;
}

async function dpopGenerateKeyPair() {
  return crypto.subtle.generateKey(
    { name: 'ECDSA', namedCurve: 'P-256' },
    false,  // private key cannot be exported
    ['sign', 'verify']
  );
}
