# Frontend integration — Personal-branding images

The backend already returns everything needed to render rich, clickable cards with image
previews. There is **no new endpoint** — the branding feature rides on the existing chat API.
This doc is the contract for the web frontend.

## Where the cards come from

Every assistant turn returns a `cards: []` array:

- **Live:** `POST /v1/chat` → response body has `reply`, `conversation_id`, `message_id`, **`cards`**.
- **History:** `GET /v1/conversations/{id}/messages` → each assistant message has a **`cards`** field
  (same shape) so re-opening a conversation re-renders the cards.

`cards` is an array of objects discriminated by `type`. The branding feature adds three types:
`branding_templates`, `branding_image`, `branding_history`. Render any other `type` as you do today.

> Image URLs (`thumbnail_url`, `image_url`) are **presigned S3 links, valid ~7 days**. Put them
> straight into `<img src>` / a download anchor. They may differ between calls (fresh signature each
> time) — don't hard-cache them; just use what's in the latest card. A field can be `null` if S3 was
> briefly unavailable — fall back to text (title/description) in that case.

---

## 1. `branding_templates` — the picker

Emitted when the agent asks to make a branding image (the assistant says something like *"Here are
some templates — tap one"*). Render a **grid of clickable cards, each showing its preview image**.

```json
{
  "type": "branding_templates",
  "templates": [
    {
      "id": "busy-selling",
      "title": "Busy Selling",
      "description": "Lounging in a leather chair with a retro phone — bold studio headline",
      "suggested_text": "Busy selling",
      "aspect_ratio": "4:5",
      "thumbnail_url": "https://assets-allegiance.s3.amazonaws.com/branding/templates/busy-selling.jpg?X-Amz-..."
    }
    // ... 12 total
  ]
}
```

| field | use |
|---|---|
| `id` | the template slug — **this is what selection must resolve to** |
| `title` | display name on the card |
| `description` | optional one-line caption under the title |
| `aspect_ratio` | `"4:5" \| "2:3" \| "3:4" \| "9:16"` — use it to size the thumbnail box so the grid doesn't jump |
| `thumbnail_url` | preview image (presigned). Render in `<img>` |
| `suggested_text` | optional — a default overlay line you can prefill if you build a custom text box |

### Selecting a template (the "click → it catches the name" part)

The flow is chat-driven. When the user taps a card, **send a normal chat message** on their behalf
with the template's `title` (or `id`). The model already saw the template list and maps it back:

```js
function onTemplatePick(t) {
  sendChatMessage(t.title);   // e.g. "Busy Selling"  (t.id also works)
}
```

The assistant will then ask **"Would you like to add a short line of text?"** (next turn). You can
render that as two buttons:

```js
onYes() => sendChatMessage("Yes");   // assistant then asks for the line; user types it (≤ 60 chars)
onNo()  => sendChatMessage("No, no text");  // → clean image, no overlay
```

(You don't have to use buttons — the user can just type. Buttons are nicer UX.)

---

## 2. `branding_image` — the finished poster (preview + download)

Emitted when generation succeeds. Render the **image preview** plus a **download button**.

```json
{
  "type": "branding_image",
  "status": "completed",
  "template_id": "busy-selling",
  "template_title": "Busy Selling",
  "has_text": true,
  "overlay_text": "Busy selling",
  "image_url": "https://assets-allegiance.s3.amazonaws.com/generated/flyers/branding-busy-selling-1a2b...png?X-Amz-...",
  "filename": "branding-busy-selling-1a2b3c4d.png",
  "sent_to_telegram": false,
  "url_expires": "7 days"
}
```

```jsx
<figure>
  <img src={card.image_url} alt={card.template_title} />
  <a href={card.image_url} download={card.filename}>Download</a>
</figure>
```

Notes:
- `image_url` is both the preview source **and** the download href.
- The assistant's `reply` text also contains a `🎨 Download image: <url>` line (the backend appends
  the byte-exact link). If you render this card, you can hide/ignore that line in the text to avoid
  duplication — your call.

---

## 3. `branding_history` — the agent's gallery (optional)

Emitted when the agent asks to see their past branding images (`action="list_history"`). Render a
small grid of past images.

```json
{
  "type": "branding_history",
  "images": [
    {
      "id": "0f2c...uuid",
      "template_id": "no-days-off",
      "template_title": "No Days Off",
      "overlay_text": "No days off",
      "image_url": "https://assets-allegiance.s3.amazonaws.com/generated/flyers/...png?X-Amz-...",
      "created_at": "2026-06-23T15:40:00+00:00"
    }
  ]
}
```

> History is only populated once the `branding_images` table migration is applied
> (`db/add_branding_images.sql`). Until then this card simply won't appear; everything else works.
> Older `image_url`s are presigned and expire after 7 days — the web app can also read the agent's
> gallery directly from the `branding_images` table (RLS: each agent sees their own rows) and
> re-presign from `s3_key` for permanent links if you prefer.

---

## Minimal renderer sketch

```jsx
function Cards({ cards, sendChatMessage }) {
  return cards.map((c) => {
    switch (c.type) {
      case "branding_templates":
        return (
          <div className="template-grid">
            {c.templates.map((t) => (
              <button key={t.id} className="template-card"
                      style={{ aspectRatio: t.aspect_ratio.replace(":", " / ") }}
                      onClick={() => sendChatMessage(t.title)}>
                {t.thumbnail_url
                  ? <img src={t.thumbnail_url} alt={t.title} />
                  : <div className="ph" />}
                <span>{t.title}</span>
              </button>
            ))}
          </div>
        );
      case "branding_image":
        return (
          <figure>
            <img src={c.image_url} alt={c.template_title} />
            <a href={c.image_url} download={c.filename}>Download</a>
          </figure>
        );
      case "branding_history":
        return (
          <div className="gallery">
            {c.images.map((im) => <img key={im.id} src={im.image_url} alt={im.template_title} />)}
          </div>
        );
      // ...your existing card types
    }
  });
}
```

## Summary of contract guarantees
- Cards arrive on both `POST /v1/chat` and `GET /v1/conversations/{id}/messages` under `cards[]`.
- `branding_templates` always has 12 entries, each with `id`, `title`, `aspect_ratio`, and (when S3
  is healthy) `thumbnail_url`.
- Selection = send the template `title` (or `id`) as the next chat message. No special endpoint.
- `branding_image.image_url` is the preview + download URL; `branding_image.filename` is the suggested
  download name.
- All image URLs are presigned and short-lived (~7 days) — render immediately, don't long-cache.
