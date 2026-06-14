# credproxy bundled script: bearer
#
# A Starlark re-implementation of the built-in `bearer` scheme -- an authoring
# template for scripted injectors (and the Python-vs-Starlark benchmark
# subject). Substring-swap the placeholder for the real value inside a named
# header (default Authorization), leaving any "Bearer "/"token " prefix intact.
#
# Reference it from a scripted injector:
#   scheme = "script"
#   script = "bearer"
#   family = "substitute"
#   slots  = ["value"]
#   [params]
#   header = "Authorization"

def on_request(ctx):
    header = param(ctx, "header", "Authorization")
    value = header_get(ctx, header)
    ph = placeholder(ctx)
    if value == None or ph == None:
        return False
    if ph not in value:
        return False
    header_set(ctx, header, value.replace(ph, secret(ctx)))
    return True
