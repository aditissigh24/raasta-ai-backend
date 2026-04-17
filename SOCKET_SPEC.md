# AI Backend Integration Spec — Wingman (Raasta)

This document is the single source of truth for the AI backend team. It covers the integration path, event types, payload schemas, expected response shapes, and DB side-effects that the AI backend must implement to integrate with the Wingman socket-server.

---

## 1. Architecture Overview

```
Client (browser)
      │  Socket.IO
      ▼
Socket-Server (Node.js / Socket.IO)  ◄──── Redis pub/sub adapter ────► Socket-Server (Node.js / Socket.IO)
      │                                    (horizontal scaling)                    │
      └── EMIT  ai:request  ─────────────────────────────────────────────────────►│
                                                                          AI Backend│
      ◄── EMIT  ai:response ─────────────────────────────────────────────────────┘
      │
      └── broadcast to user room  ───────────────────────► Client
```

**Single integration path — Socket.IO direct connection:**

The AI backend connects as a privileged Socket.IO client (`ai_worker`). The socket-server emits `ai:request` events to the AI backend; the AI backend responds by emitting `ai:response` back on the same connection. A Redis pub/sub adapter is attached to the socket-server so that `io.to(room).emit(...)` works across all server instances — enabling horizontal scaling without any changes to the AI backend or browser client code.

---

## 2. Socket.IO Connection

### 2.1 Connecting as an AI Worker

The AI backend connects to the same socket-server URL that clients use, but authenticates with a special `role` and `secret`:

```js
import { io } from "socket.io-client";

const socket = io(process.env.SOCKET_SERVER_URL, {
  auth: {
    role: "ai_worker",
    secret: process.env.AI_WORKER_SECRET, // must match socket-server AI_WORKER_SECRET env var
  },
  transports: ["websocket"],
  reconnection: true,
  reconnectionDelay: 1000,
  reconnectionDelayMax: 10000,
});

socket.on("connect", () => console.log("AI worker connected:", socket.id));
socket.on("connect_error", (err) =>
  console.error("AI worker connection failed:", err.message),
);
```

On successful connection the socket-server automatically joins this socket to the `ai:workers` room. No additional room-join event is needed.

**Environment variable required on socket-server:**

```
AI_WORKER_SECRET=<your-shared-secret>
```

---

### 2.2 Receiving Requests — `ai:request` event

The socket-server emits `ai:request` to the `ai:workers` room for every message that needs AI processing.

```js
socket.on("ai:request", (payload) => {
  console.log("Received AI request:", payload.type, payload.messageId);
  // ... process and respond
});
```

See §4 for the full payload schema for each `type` value.

---

### 2.3 Sending Responses — `ai:response` event

Emit `ai:response` on the AI backend's socket. The socket-server will route it to the correct user room and handle all DB writes.

```js
socket.emit("ai:response", {
  type: "onboarding",
  userId: payload.userId,
  roomId: payload.roomId,
  messageId: payload.messageId,   // REQUIRED — used to cancel typing timeout
  sessionId: payload.sessionId,
  replyText: "Tera naam kya hai bhai?",
  options: [...],
  onboardingComplete: false,
  // ...
});
```

See §5 for the full response payload schema for each `type` value.

---

## 3. Presence Events — `user:session` event

The socket-server emits `user:session` to the `ai:workers` room to track user presence (useful for managing conversation context, clearing stale state, etc.).

```js
socket.on("user:session", (data) => {
  if (data.event === "connected") {
    // user came online
  } else if (data.event === "disconnected") {
    // user went offline
  }
});
```

**Payload shapes:**

```json
{
  "event": "connected",
  "userId": "<cuid>",
  "timestamp": "2026-04-15T10:30:00.000Z"
}
```

```json
{
  "event": "disconnected",
  "userId": "<cuid>",
  "reason": "transport close",
  "timestamp": "2026-04-15T10:35:00.000Z"
}
```

**Notes:**

- `connected` fires only when the user's **first** socket connects (not on multi-tab reconnects).
- `disconnected` fires only when the user's **last** socket closes (user is truly offline).

---

## 4. Receiving: `ai:request` — Payload Types

The AI backend receives all requests via the `ai:request` Socket.IO event. Handle the following `type` values.

### 4.1 `onboarding_start`

Triggered when a new user completes OTP auth and the onboarding chat room is joined. The AI should respond with a greeting and ask for the user's name.

```json
{
  "type": "onboarding_start",
  "messageId": "msg_1713000000000_a1b2c3d4",
  "sessionId": "onboarding_<userId>_2026-04-15",
  "userId": "<cuid>",
  "roomId": "onboarding:user:<userId>",
  "text": "__START__",
  "characterId": null,
  "scenarioId": null
}
```

**AI should respond with:** A warm greeting in Hinglish, ask for the user's name.
Example: _"Hey! Main hoon tera Wingman 🤝 Pehle bata — tera naam kya hai?"_

---

### 4.2 `onboarding`

Triggered for every subsequent message during the onboarding conversation (after `onboarding_start`). The AI drives the full conversation: name → age range → current situation → scenario recommendation.

```json
{
  "type": "onboarding",
  "messageId": "msg_1713000001000_e5f6a7b8",
  "sessionId": "onboarding_<userId>_2026-04-15",
  "userId": "<cuid>",
  "roomId": "onboarding:user:<userId>",
  "text": "<user's message or selected option id>",
  "characterId": null,
  "scenarioId": null
}
```

**Conversation flow the AI must drive:**

1. Collect **name** → send `name_collected` side-effect (see §6)
2. Ask for **age range** (options: `18-21`, `22-25`, `26-30`, `30+`) → send `age_collected`
3. Ask about **current situation** (e.g. wants to approach someone, already texting someone, had a date go badly)
4. Based on name + age + situation, **recommend 2–3 scenarios** from the DB (send scenario IDs as options)
5. When user picks a scenario → send `onboarding_complete` response (see §5.2)

---

### 4.3 `roleplay_start`

Triggered when a user joins a roleplay room (either from onboarding transition or home screen scenario card click). The AI should send the character's opening line.

```json
{
  "type": "roleplay_start",
  "messageId": "msg_1713000002000_c9d0e1f2",
  "sessionId": "roleplay_<userId>_<scenarioId>_2026-04-15",
  "conversationId": "<cuid>",
  "userId": "<cuid>",
  "roomId": "roleplay:<scenarioId>:<characterId>:user:<userId>",
  "scenarioId": "<e.g. S01>",
  "characterId": "<e.g. C01>",
  "text": "__START__"
}
```

**AI should respond with:** The character's first message in-character, as if the scenario has just begun. Optionally include 2–3 response choices for the user in `options`.

---

### 4.4 `roleplay`

Triggered for every user message during an active roleplay session.

```json
{
  "type": "roleplay",
  "messageId": "msg_1713000003000_g3h4i5j6",
  "sessionId": "session_<conversationId>_2026-04-15",
  "conversationId": "<cuid>",
  "userId": "<cuid>",
  "roomId": "roleplay:<scenarioId>:<characterId>:user:<userId>",
  "scenarioId": "<e.g. S01>",
  "characterId": "<e.g. C01>",
  "text": "<user's message or selected choice id>"
}
```

**AI should respond with:** The character's in-character reply. Optionally include `wingmanTip` for coaching, and `options` for the first exchange.

---

## 5. Sending: `ai:response` — Response Types

The AI backend sends all responses by emitting `ai:response` on its socket. The socket-server routes the payload to the correct user room and handles all DB writes.

### 5.1 Standard onboarding response

```json
{
  "type": "onboarding",
  "userId": "<cuid>",
  "roomId": "onboarding:user:<userId>",
  "messageId": "<echo the messageId from the request>",
  "sessionId": "<echo the sessionId>",
  "replyText": "Tera naam kya hai bhai?",
  "options": [
    { "id": "opt_1", "label": "18-21", "subtext": "College / fresher" },
    { "id": "opt_2", "label": "22-25", "subtext": "Job lag gayi" }
  ],
  "onboardingComplete": false,
  "characterId": null,
  "scenarioId": null,
  "conversationId": null,
  "wingmanTip": null,
  "isUserMessage": false
}
```

**Field reference:**

| Field                | Type           | Required | Notes                                                             |
| -------------------- | -------------- | -------- | ----------------------------------------------------------------- |
| `type`               | `"onboarding"` | Yes      | Identifies message as onboarding turn                             |
| `userId`             | string         | Yes      | Echo from request                                                 |
| `roomId`             | string         | Yes      | Echo from request — determines which Socket.IO room receives this |
| `messageId`          | string         | Yes      | Echo from request — used for timeout cleanup                      |
| `sessionId`          | string         | Yes      | Echo from request                                                 |
| `replyText`          | string         | Yes      | The AI's message text                                             |
| `options`            | array or null  | No       | Tap-to-reply chips. Each: `{ id, label, subtext? }`               |
| `onboardingComplete` | boolean        | No       | `false` during conversation                                       |
| `characterId`        | string or null | No       | null during onboarding                                            |
| `scenarioId`         | string or null | No       | null during onboarding                                            |
| `conversationId`     | string or null | No       | null during onboarding                                            |
| `wingmanTip`         | string or null | No       | Optional coaching tip                                             |
| `isUserMessage`      | boolean        | No       | Always `false` for AI responses                                   |

---

### 5.2 Onboarding complete response

Sent when the user has selected a scenario and the AI is ready to hand off to roleplay.

```json
{
  "type": "onboarding_complete",
  "userId": "<cuid>",
  "roomId": "onboarding:user:<userId>",
  "messageId": "<echo messageId>",
  "sessionId": "<echo sessionId>",
  "replyText": "Perfect choice! Chalte hain Simran ke saath 🔥",
  "options": null,
  "onboardingComplete": true,
  "scenarioId": "S07",
  "characterId": "C02",
  "conversationId": null,
  "wingmanTip": null,
  "isUserMessage": false
}
```

**Critical fields:**

- `onboardingComplete: true` — triggers the client to start the `ScenarioTransition` animation
- `scenarioId` — must be a valid scenario ID from the `Scenario` table
- `characterId` — must be the character associated with that scenario

---

### 5.3 Roleplay start response

Sent in response to `roleplay_start`. Character's opening line.

```json
{
  "type": "roleplay_start",
  "userId": "<cuid>",
  "roomId": "roleplay:<scenarioId>:<characterId>:user:<userId>",
  "messageId": "<echo messageId>",
  "sessionId": "<echo sessionId>",
  "conversationId": "<echo conversationId>",
  "replyText": "Omg finally tune message kiya 😤 Maine socha tu kabhi nahi karega.",
  "options": [
    { "id": "opt_a", "label": "Haha sorry yaar, busy tha" },
    { "id": "opt_b", "label": "Miss kar raha tha isko 😏" },
    { "id": "opt_c", "label": "Kya matlab busy? Tu busy hai mere liye?" }
  ],
  "onboardingComplete": false,
  "scenarioId": "S07",
  "characterId": "C02",
  "wingmanTip": null,
  "isUserMessage": false
}
```

---

### 5.4 Roleplay turn response

Sent in response to each `roleplay` message.

```json
{
  "type": "roleplay",
  "userId": "<cuid>",
  "roomId": "roleplay:<scenarioId>:<characterId>:user:<userId>",
  "messageId": "<echo messageId>",
  "sessionId": "<echo sessionId>",
  "conversationId": "<echo conversationId>",
  "replyText": "Haha okay okay, maafi qabool 😄 Kya kar raha tha?",
  "options": null,
  "onboardingComplete": false,
  "scenarioId": "S07",
  "characterId": "C02",
  "wingmanTip": "Good move — tune humour se tension break kiya. Keep it light!",
  "isUserMessage": false
}
```

**`wingmanTip`:** Optional coaching note shown as an in-chat "Coach" bubble. Use it to teach the user what went right or wrong with their last message.

---

## 6. Side-Effect Response Types (Optional but Recommended)

These types do **not** emit anything to the client — the socket-server handles them purely as DB write signals. Emitting them keeps the user's profile up to date for personalisation.

### 6.1 `name_collected`

```json
{
  "type": "name_collected",
  "userId": "<cuid>",
  "messageId": "<echo messageId>",
  "name": "Rahul"
}
```

Socket-server effect: `UPDATE User SET name = 'Rahul' WHERE id = userId`

---

### 6.2 `age_collected`

```json
{
  "type": "age_collected",
  "userId": "<cuid>",
  "messageId": "<echo messageId>",
  "ageRange": "22-25"
}
```

Socket-server effect: `UPDATE User SET ageRange = '22-25' WHERE id = userId`

---

### 6.3 `scenario_selected`

```json
{
  "type": "scenario_selected",
  "userId": "<cuid>",
  "messageId": "<echo messageId>",
  "scenarioId": "S07",
  "characterId": "C02"
}
```

Socket-server effect: `UPDATE User SET preferredScenarioId = 'S07' WHERE id = userId`

---

## 7. Room Naming Conventions

| Context    | Room format                                         |
| ---------- | --------------------------------------------------- |
| Onboarding | `onboarding:user:{userId}`                          |
| Roleplay   | `roleplay:{scenarioId}:{characterId}:user:{userId}` |

Always echo the `roomId` from the request in your response — the socket-server uses it to route the response to the right Socket.IO room.

---

## 8. Timeout & Error Behaviour

- The socket-server starts a **30-second typing timeout** immediately after emitting each `ai:request`.
- If the AI backend does **not** respond within 30 seconds, the socket-server clears the typing indicator automatically.
- The `messageId` in your response is used to cancel this timeout — always echo it accurately.
- If the AI backend encounters an error, do **not** emit a response. The timeout will handle the UX gracefully.

---

## 9. Message ID Tracking

To cancel the typing timeout on the socket-server side, your response payload **must** include `messageId` matching the request. Without it, the typing indicator will persist for 30s before clearing automatically.

---

## 10. Data Models Reference

Key IDs available in the Postgres DB for scenario/character lookup:

- `Scenario.id` — `VarChar(10)`, e.g. `"S01"`, `"S07"`
- `Character.id` — `VarChar(10)`, e.g. `"C01"`, `"C02"`
- `Chapter.id` — `VarChar(10)`, e.g. `"CH1"`, `"CH2"` (groups scenarios by theme)
- `User.id` — CUID, e.g. `"clxxxxxxxxxxxxxxx"`
- `Conversation.id` — CUID (created by socket-server on `join_roleplay`)

The AI backend has read access to all scenario/character data needed to personalise recommendations. The socket-server handles all Prisma writes; the AI backend communicates write intent through side-effect response types (§6).
