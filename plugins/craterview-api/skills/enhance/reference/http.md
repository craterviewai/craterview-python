# The API over HTTP, from a shell

For a shell script, a CI step, a language the clients do not cover, or reproducing a
problem with nothing in the way. The same three calls, with `CV_API_KEY` in the environment
and `jq` for the fields the next step needs:

```bash
# 1 — ask for somewhere to put the file. content_type and content_length are required,
#     and both are signed into the URL that comes back.
LEN=$(wc -c < photo.jpg)
SLOT=$(curl -sS -X POST "https://api.craterview.ai/v1/uploads" \
  -H "Authorization: Bearer $CV_API_KEY" \
  -H "Content-Type: application/json" \
  -d "{\"content_type\": \"image/jpeg\", \"content_length\": $LEN}")
UPLOAD_URL=$(echo "$SLOT" | jq -r .upload_url)
INPUT_KEY=$(echo "$SLOT" | jq -r .input_key)

# 2 — upload the bytes to storage, not to the API. The content type must be the one
#     declared above: it is part of the signature, and a mismatch is refused by storage.
curl -sS -f -X PUT "$UPLOAD_URL" \
  -H "Content-Type: image/jpeg" \
  --data-binary @photo.jpg

# 3 — submit. ?wait=25 holds the response open so a fast job comes back finished;
#     a slower one comes back queued or running with an id to read.
JOB=$(curl -sS -X POST "https://api.craterview.ai/v1/jobs?wait=25" \
  -H "Authorization: Bearer $CV_API_KEY" \
  -H "Content-Type: application/json" \
  -d "{\"model\": \"cv-enhance-v3\", \"input_key\": \"$INPUT_KEY\", \"params\": {\"scale\": 4}}")
JOB_ID=$(echo "$JOB" | jq -r .id)

# 4 — read until settled, then fetch the result from the link the job carries.
until [ "$(echo "$JOB" | jq -r .status)" = succeeded ] || [ "$(echo "$JOB" | jq -r .status)" = failed ]; do
  sleep 2
  JOB=$(curl -sS "https://api.craterview.ai/v1/jobs/$JOB_ID" -H "Authorization: Bearer $CV_API_KEY")
done
echo "$JOB" | jq -r '.status, .error // empty'
# -OJ saves under the name the link carries, whose suffix is the format that came back.
curl -sS -f -OJ "$(echo "$JOB" | jq -r .result.output.download_url)"
```

What goes wrong here, in order of how often: `-d` without the JSON `Content-Type` header
arrives form-encoded and is refused; a PUT whose `Content-Type` differs from the declared
one is refused by storage, with an error that does not mention CraterView.ai; an
`Authorization` header on the upload or on a result link — the API key, sent because the
client adds it to every request — is refused by storage with `400` and
`Missing x-amz-content-sha256`, because those links are signed already; a `wait` longer
than the specification allows is a 422 rather than a longer wait; and the result links
expire, so fetch the file rather than keeping the URL. A refusal is
`{"detail", "code"}`: branch on `code`, which is stable, never on the sentence. Each model's
parameters, price and limits are in the catalogue, `GET /v1/models`; every request and response
shape is in the specification at https://api.craterview.ai/openapi.json. Neither is in this
page.
