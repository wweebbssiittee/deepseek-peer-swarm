# A2A interface

This harness implements the text and task-polling subset of the
[A2A 1.0 JSON-RPC binding](https://a2a-protocol.org/latest/specification/).
The collective card is at `/.well-known/agent-card.json`. Each of the ten peers
also has `/a2a/agents/peer-01/.well-known/agent-card.json` through `peer-10`.

POST requests go to `/a2a` or `/a2a/agents/peer-01` through `peer-10`. Send
`A2A-Version: 1.0`, `Content-Type: application/json`, and
`Authorization: Bearer <local harness token>`. This token is separate from the
DeepSeek keys. Agent cards are public; task endpoints require authentication.

Create a run and grant its permissions through the local dashboard first. To
add direction to that existing run, send:

```json
{
  "jsonrpc": "2.0",
  "id": "request-1",
  "method": "SendMessage",
  "params": {
    "message": {
      "messageId": "unique-message-id",
      "role": "ROLE_USER",
      "taskId": "REPLACE_WITH_RUN_ID",
      "contextId": "REPLACE_WITH_RUN_ID",
      "parts": [{"text": "Please prioritize verifying the experiment results."}]
    },
    "configuration": {"returnImmediately": true}
  }
}
```

The response contains `result.task` or `result.message`. Poll with `GetTask`
and `{"id":"REPLACE_WITH_RUN_ID"}`. `CancelTask` takes the same ID;
`ListTasks` supports filters and pagination. Without `returnImmediately: true`,
`SendMessage` waits for completion or a state requiring input/authorization.

Streaming, push notifications, binary/data parts and extended cards are not
implemented. Legacy 0.3 methods and shapes are not advertised or accepted.

Internal peers use the same validated JSON-RPC dispatcher and durable backend
in process. Incoming metadata cannot establish a trusted peer identity. The
runtime supplies that identity separately when dispatching internal messages.
