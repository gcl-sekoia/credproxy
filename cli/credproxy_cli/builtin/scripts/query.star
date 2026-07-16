# credproxy builtin script: query
#
# A Starlark re-implementation of the built-in `query` scheme -- an authoring
# template. Substring-swap the placeholder for the real value inside the URL
# query string (Shodan-style `?key=…` APIs).
#
# Scoped to the query portion (after the first `?`) so a placeholder-shaped
# substring in the path is left alone. Unlike the native scheme this does NOT
# percent-encode the value -- there is no encode primitive, and it is a no-op
# for the URL-unreserved default placeholder charset; a key with `&`/`=`/`#`
# would need a sign-family script that composes the encoding itself.

def on_request():
    target = req_path()
    ph = placeholder()
    if ph == None or "?" not in target:
        return False
    head, sep, query = target.partition("?")
    if ph not in query:
        return False
    req_set_path(head + sep + query.replace(ph, secret()))
    return True
