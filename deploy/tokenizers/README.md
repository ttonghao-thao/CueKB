# Registered delivery tokenizers

Place the exact VoiceChat tokenizer JSON in this directory on the deployment host and set:

```text
CUEKB_EVIDENCE_VOICE_TOKENIZER_PATH=/etc/cuekb/tokenizers/<file>.json
CUEKB_EVIDENCE_VOICE_TOKENIZER_ID=<immutable model/tokenizer revision>
```

The file is loaded locally by the API on the first `delivery_profile=voice` request. Do not
store licensed model assets in Git. A missing, unreadable or mismatched registration causes an
explicit 409 response; CueKB does not substitute character or word estimates.
