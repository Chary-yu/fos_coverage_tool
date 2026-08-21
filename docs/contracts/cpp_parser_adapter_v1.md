# C++ parser adapter v1

The VNext inheritance engine consumes a deterministic parser adapter. The
production adapter is selected explicitly; a discovered executable is not
treated as proof that the adapter is usable.

The registered `json-cli-v1` adapter starts the configured helper with
`--analyze-json`, writes one request to stdin, and expects one JSON object on
stdout. The request is:

```json
{
  "protocol": "coverage-cpp-parser-v1",
  "path": "src/example.cpp",
  "source": "int f() { return 0; }\n"
}
```

The response may contain the analysis directly or wrap it in an `analysis`
property. It must contain `functions`; each function has `name`, `scope`,
`parameters`, `qualifiers`, `trailing_return`, `start_line`, and `end_line`.
`controls`, `preprocessor`, `macros`, `constants`, and `calls` are objects
keyed by physical line number. A parser that cannot determine a range must
return `uncertain: true` or omit that range so the engine produces an ordinary
pending decision.

A configured helper must pass both:

1. `<command> --version`; and
2. `<command> --analyze-json` against a small C fixture, including conversion
   to the engine's `FunctionIdentity`/`FunctionRange` objects.

Configure it under `inheritance_parser` (or `inheritance.parser`):

```json
{
  "inheritance_parser": {
    "adapter": "json-cli-v1",
    "command": ["/opt/fos/bin/cpp-parser"],
    "require_external": true
  }
}
```

If the helper is absent, malformed, emits the wrong protocol, or fails the
smoke test, runtime construction fails closed and Gate D/F remains incomplete.
