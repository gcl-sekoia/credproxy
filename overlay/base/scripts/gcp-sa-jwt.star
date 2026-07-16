# credproxy script: gcp-sa-jwt
#
# Google Cloud service-account self-signed JWT (RS256). Mints a fresh JWT on
# every matching request, signed with the SA's private key, and sets
# Authorization: Bearer <jwt>. See gcp-sa-jwt.toml for the slot/param contract.
#
# family = "sign": the private key never transits the wire; the proxy signs.
#
# Slots (all from the SA key JSON):
#   private_key   - PEM RSA private key (PKCS#8/PKCS#1)
#   key_id        - the SA's private_key_id -> the JOSE header `kid`
#   client_email  - the SA email           -> the `iss`/`sub` claims
#
# Params (optional):
#   aud - audience; empty (default) derives https://<host>/ per request
#   ttl - lifetime in seconds (default "3600")

def on_request():
    # Optional placeholder gate. With no placeholder (the usual GCP case) the
    # proxy mints on EVERY matching request: the workspace sends an
    # unauthenticated REST request (AnonymousCredentials) and the proxy stamps
    # it. Set a placeholder on the binding to gate minting to requests carrying
    # `Authorization: Bearer <placeholder>` (per-request opt-in / multiple SAs
    # on one host).
    ph = placeholder()
    if ph != None:
        auth = req_header("Authorization")
        if auth == None or ph not in auth:
            return False

    iat = now()
    exp = iat + int(param("ttl", "3600"))

    email = secret("client_email")

    # GCP self-signed JWT audience is the service root. Derive it from the target
    # host unless pinned, so one binding covers every *.googleapis.com service.
    aud = param("aud", "")
    if aud == "":
        aud = "https://" + req_host() + "/"

    # `kid` rides the JOSE header (NOT the claims) and MUST equal the SA key's
    # private_key_id, or Google can't select the public key to verify with.
    header = {"alg": "RS256", "typ": "JWT", "kid": secret("key_id")}
    claims = {"iss": email, "sub": email, "aud": aud, "iat": iat, "exp": exp}

    # jwt_encode_sign owns the JWS assembly (header.claims.signature), base64url
    # padding, and signing exactly the right bytes -- the JWS footguns.
    jwt = jwt_encode_sign(header, claims, secret("private_key"))
    req_set_header("Authorization", "Bearer " + jwt)
    return True
