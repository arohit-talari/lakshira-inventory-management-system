# =============================================================================
# LAKSHIRA — PRODUCT DESCRIPTION GENERATOR
# =============================================================================
# Generates Instagram/Shopify/WhatsApp product descriptions from a unit's
# known facts, the founder's own raw notes, and optional photos. Grounded in
# Brand_Context_Checklist.md (+ both follow-up rounds) and
# Weave_Type_Reference.md, both loaded fresh on every call so an edit takes
# effect on the next run with no restart needed -- same pattern as
# generate_report.py's _load_brand_context().
#
# The generation itself is a real multi-turn conversation, not one-shot: the
# full message history (her notes, every version shown, every refinement
# instruction) is kept in memory for the life of the operation, so a
# refinement like "keep what version 2 said about the border, expand on the
# zari" resolves against the actual transcript rather than a guess.

import os
import re
import base64
import mimetypes
from datetime import date

from report_config import ANTHROPIC_API_KEY

# The service account has no Drive storage quota of its own (Google
# service accounts never do) -- it can only write into a folder a real
# Google account already owns and has explicitly shared with it as an
# Editor. DRIVE_PHOTOS_ROOT_ID is that folder's ID (from its URL:
# drive.google.com/drive/folders/<this>), set once in .env.
DRIVE_PHOTOS_ROOT_ID = os.environ.get("DRIVE_PHOTOS_ROOT_ID", "").strip()

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DOCS_DIR = os.path.join(BASE_DIR, "..", "docs")
CREDS_PATH = os.path.join(BASE_DIR, "credentials.json")

_BRAND_CONTEXT_FILES = [
    "Brand_Context_Checklist.md",
    "Brand_Context_Followup_Questions.md",
    "Brand_Context_Followup_Questions_2.md",
]
_WEAVE_TYPE_REFERENCE_PATH = os.path.join(DOCS_DIR, "Weave_Type_Reference.md")

MODEL = "claude-opus-5"

DRIVE_SCOPE = ["https://www.googleapis.com/auth/drive"]

# Weave type -> family, matching Weave_Type_Reference.md's grouping exactly.
# Anything not listed here has no Pass-1 family entry yet (the 6 standalone
# traditions + blouse/dupatta-only types) -- _weave_type_context() degrades
# gracefully for those, same principle as a missing brand context file.
WEAVE_FAMILIES = {
    "Kanjivaram": "Kanjivaram", "Twill Kanjivaram": "Kanjivaram",
    "Krishnamoorthy Kanjivaram Silk": "Kanjivaram", "Zari Kota Kanjivaram Silk": "Kanjivaram",
    "Ikat Kanjivaram Silk": "Kanjivaram", "Bamboo Bandej Kanjivaram Silk": "Kanjivaram",
    "Khadi Kanjivaram Silk": "Kanjivaram", "Finest Contemporary Kanjivaram Silk": "Kanjivaram",
    "Kanjivaram Dupatta": "Kanjivaram", "Kanjivaram Silk Saree - Digital Print": "Kanjivaram",
    "Kanjivaram Korvai Kora Silk": "Kanjivaram", "Kanjivaram Silk Blouse": "Kanjivaram",
    "Kuttu Gadwal Silk": "Gadwal", "Twill Kuttu Gadwal Silk": "Gadwal", "Twill Gadwal Silk": "Gadwal",
    "Benaras": "Benaras", "Benaras Kota": "Benaras", "Benaras Ektara Varnasi": "Benaras",
    "Ikat Silk": "Ikat", "Ikat Silk Blouse": "Ikat",
    "Chanderi": "Chanderi",
}


# -----------------------------------------------------------------------
# Context loading -- brand voice + weave-type reference, each degrading
# independently so a missing file never crashes the operation.
# -----------------------------------------------------------------------

def _load_brand_context():
    parts = []
    for filename in _BRAND_CONTEXT_FILES:
        try:
            with open(os.path.join(DOCS_DIR, filename), "r", encoding="utf-8") as f:
                content = f.read().strip()
        except OSError:
            continue
        if content:
            parts.append(content)
    return "\n\n---\n\n".join(parts)

def _weave_type_context(weave_type):
    """Pull just the relevant family + sub-style sections out of
    Weave_Type_Reference.md, not the whole file -- keeps the prompt focused
    on this unit's actual weave type instead of 20 irrelevant entries.
    Returns "" if the file is missing or this weave type has no Pass-1
    entry yet (the generator leans harder on brand context and her notes
    in that case, same graceful-degradation principle as everywhere else)."""
    family = WEAVE_FAMILIES.get(weave_type)
    if not family:
        return ""
    try:
        with open(_WEAVE_TYPE_REFERENCE_PATH, "r", encoding="utf-8") as f:
            text = f.read()
    except OSError:
        return ""

    sections = re.split(r"(?=^## )", text, flags=re.MULTILINE)
    family_section = next((s for s in sections if s.startswith(f"## ") and family in s.split("\n", 1)[0]), "")
    if not family_section:
        return ""
    if weave_type == family:
        # Family baseline entry only -- strip any nested sub-style (###)
        # entries so we're not sending every sibling sub-style too.
        return re.split(r"(?=^### )", family_section, flags=re.MULTILINE)[0].strip()

    sub_sections = re.split(r"(?=^### )", family_section, flags=re.MULTILINE)
    baseline = sub_sections[0].strip()
    sub_entry = next((s for s in sub_sections[1:] if weave_type in s.split("\n", 1)[0]), "")
    return (baseline + ("\n\n" + sub_entry.strip() if sub_entry else "")).strip()


# -----------------------------------------------------------------------
# Google Drive -- photo storage. Same service account already used for
# Sheets already carries Drive scope, so no new credential is needed.
# -----------------------------------------------------------------------

_drive_cache = None
OAUTH_CLIENT_SECRET_PATH = os.path.join(BASE_DIR, "oauth_client_secret.json")
DRIVE_TOKEN_PATH = os.path.join(BASE_DIR, "drive_token.json")

def _drive_service():
    """Google service accounts have no Drive storage quota of their own and
    (as of this writing) cannot create files even in a folder a real
    personal account has shared with them -- confirmed directly against
    the live API, not assumed. So Drive access here is genuine OAuth user
    delegation instead: the code acts as the developer's own Google
    account, the same way any "Sign in with Google" app would, not as
    the service account
    used for Sheets. drive_token.json holds the refresh token after the
    one-time browser authorization and is reused (and auto-refreshed)
    silently on every call after that."""
    global _drive_cache
    if _drive_cache is not None:
        return _drive_cache
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from google.auth.transport.requests import Request
    from googleapiclient.discovery import build

    creds = None
    if os.path.exists(DRIVE_TOKEN_PATH):
        creds = Credentials.from_authorized_user_file(DRIVE_TOKEN_PATH, DRIVE_SCOPE)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(OAUTH_CLIENT_SECRET_PATH, DRIVE_SCOPE)
            creds = flow.run_local_server(port=0)
        with open(DRIVE_TOKEN_PATH, "w") as f:
            f.write(creds.to_json())

    _drive_cache = build("drive", "v3", credentials=creds, cache_discovery=False)
    return _drive_cache

def _get_or_create_folder(drive, name, parent_id=None):
    query = f"name = '{name}' and mimeType = 'application/vnd.google-apps.folder' and trashed = false"
    if parent_id:
        query += f" and '{parent_id}' in parents"
    results = drive.files().list(q=query, fields="files(id)").execute().get("files", [])
    if results:
        return results[0]["id"]
    metadata = {"name": name, "mimeType": "application/vnd.google-apps.folder"}
    if parent_id:
        metadata["parents"] = [parent_id]
    folder = drive.files().create(body=metadata, fields="id").execute()
    return folder["id"]

def upload_unit_photos(sku, photo_paths):
    """Uploads each path in photo_paths into a per-SKU folder under
    DRIVE_PHOTOS_ROOT_ID (creating the SKU subfolder if this is the first
    upload for that SKU), shares it as view-only for anyone with the
    link, and returns the folder's URL. Accumulates across calls -- a
    second session adding more photos for the same SKU lands in the same
    folder, not a new one."""
    if not DRIVE_PHOTOS_ROOT_ID:
        raise RuntimeError(
            "DRIVE_PHOTOS_ROOT_ID is not set in .env -- create a folder in a "
            f"real Google account's Drive, share it with {os.environ.get('GOOGLE_SERVICE_ACCOUNT_EMAIL', '(service account email)')} "
            "as Editor, and set DRIVE_PHOTOS_ROOT_ID to that folder's ID."
        )
    from googleapiclient.http import MediaFileUpload
    drive = _drive_service()
    sku_folder_id = _get_or_create_folder(drive, sku, parent_id=DRIVE_PHOTOS_ROOT_ID)

    # Drive doesn't enforce unique filenames within a folder, and this
    # function accumulates across sessions by design -- without this check,
    # dragging in the same photo for a second channel's generation (or
    # twice in one drag) would silently create a duplicate file rather than
    # reusing what's already there.
    existing_names = {
        f["name"] for f in drive.files().list(
            q=f"'{sku_folder_id}' in parents and trashed = false",
            fields="files(name)",
        ).execute().get("files", [])
    }

    for path in photo_paths:
        name = os.path.basename(path)
        if name in existing_names:
            continue
        media = MediaFileUpload(path, mimetype=mimetypes.guess_type(path)[0] or "application/octet-stream")
        drive.files().create(
            body={"name": name, "parents": [sku_folder_id]},
            media_body=media, fields="id",
        ).execute()
        existing_names.add(name)

    try:
        drive.permissions().create(
            fileId=sku_folder_id, body={"type": "anyone", "role": "reader"},
        ).execute()
    except Exception:
        pass  # sharing failure shouldn't block the upload itself

    folder = drive.files().get(fileId=sku_folder_id, fields="webViewLink").execute()
    return folder["webViewLink"]


# -----------------------------------------------------------------------
# Claude generation -- a real multi-turn session, not one-shot.
# -----------------------------------------------------------------------

CHANNEL_SPECS = {
    "instagram": (
        "INSTAGRAM: match Lakshira's existing real caption structure exactly, even "
        "though it's more rigid than a typical caption:\n"
        "1. A title line naming the piece and its collection (e.g. \"Raktima "
        "Marakata -- Aalayam Collection.\"), only if a name/collection was given -- "
        "never invent one if it wasn't.\n"
        "2. Two prose paragraphs, no bullet points -- one on body/color/material, "
        "one on border/pallu/technique. Use specific, confident technique names "
        "(korvai, oosi lines, meenakari, kattam, etc.) the way an expert would, "
        "without stopping to define them for the reader.\n"
        "3. A short factual closing line or two -- zari purity, whether falls/"
        "blouse are attached or separate, base material -- only using what's "
        "actually been stated; never invent these specifics.\n"
        "4. The SKU on its own line.\n"
        "5. A closing call to action matching her own convention, e.g. \"Please "
        "DM to buy! Ships from [wherever, if known].\"\n"
        "Never state or imply a specific price anywhere in this caption -- by "
        "house convention, price is only ever discussed after a customer DMs to "
        "ask, never pre-empted in the post itself."
    ),
    "shopify": (
        "SHOPIFY: a structured product description for someone already considering "
        "a purchase -- open with a short narrative paragraph, then a brief factual "
        "list (weave type, material, technique/craftsmanship, occasion fit) drawn "
        "only from what's known or stated, never invented. Plain text only for the "
        "whole thing, including that list -- no Markdown or asterisk-style bolding "
        "(e.g. **Weave:**). This has to be ready to paste directly with nothing "
        "added afterward, and Markdown syntax doesn't render as formatting in a "
        "plain-text destination -- it just shows up as literal asterisks. Never "
        "state a specific price in this text -- that's handled by Shopify's own "
        "price field, not the description."
    ),
    "whatsapp": (
        "WHATSAPP: short, warm, and personal, written as if messaging one specific "
        "customer directly about this piece -- first person, conversational, no "
        "marketing-copy distance. Never state a specific price here either, even "
        "though this is a personal message -- by house convention price is "
        "discussed directly in conversation, not pre-written into a description."
    ),
}

_INSTAGRAM_EXAMPLES = """Two illustrative examples of the target Instagram voice, to match the tone exactly, not just approximate it (fictional units -- not real inventory):

---
Meenal Ranga -- Kavya Collection.

A saree like this earns its stillness in a room the moment it catches light. Woven in a deep peacock teal Korvai Kanjivaram, the body carries a soft, luminous silk sheen that settles rather than shouts, on a dense high-twist mulberry silk with real structure and weight to its drape.

The border is where the craft actually shows itself: a warm antique-gold korvai join worked in a contrasting weft, interlocked into the body during weaving rather than stitched on after, so the color break reads clean and deliberate rather than pieced together. Fine, delicate, gently glowing zari buttis are set into the border at even intervals.

All zari used is of 4gm purity.
Falls is attached and blouse piece is separated. Woven in pure silk and zari.

LAH-KKVSV501
Please DM to buy! Ships from USA.
---
Suvarna Lahari -- Kavya Collection.

A saree like this earns its place the moment the checks catch light. Woven in a checked Kanjivaram body that moves through emerald, copper, and soft ivory in even blocks, the lustrous silk carries that same graceful sheen, festive without ever tipping into loud.

Traditional temple motifs run along the border in fine gold zari, picked up again in scattered buttis across the checks, so the eye keeps finding a reason to look twice.

All zari used is of 3gm purity.
Falls are attached, and the blouse piece is separated. Woven in pure silk and zari.

LAH-KKVSV512
Please DM to buy! Ships from INDIA.
---

These two examples set the bar for voice and craft, not a template to copy sentence-for-sentence. Match what they do well: stating the feeling or judgment before the fact that earns it, and treating a construction or technique detail as evidence of skill or heritage rather than a bare spec. Two things neither example does, that the copy should still do: land a closing beat that bridges the emotional thread into the CTA, rather than cutting straight from the last narrative sentence to "Please DM to buy" with nothing in between; and, where her notes or the unit facts genuinely support it, let the copy suggest who this piece is for or what moment it belongs to, not just describe the object in isolation -- always grounded in what's actually there, never invented just to fill this in. Avoid what these examples sometimes lapse into: stacking several decorative adjectives onto one noun when a single precise word would land harder, reaching for the same sheen/drape language regardless of the piece, and reusing either example's literal opening sentence shape across different units."""

_ANTI_AI_TELL = (
    "Avoid generic AI-written phrasing: no \"elevate\", \"nestled\", \"boasts\", "
    "\"isn't just X, it's Y\", \"perfect blend of\", and no em dashes used as a "
    "default habit rather than where a sentence genuinely calls for one. Write in "
    "the founder's actual voice per the brand context below, not generic luxury-"
    "retail copy."
)

_VALUE_JUSTIFICATION = (
    "Wherever the underlying facts support it, let the copy itself communicate why "
    "the piece is worth its price -- a specific technique, the hand-work involved, "
    "the rarity or quality of the material -- woven naturally into the narrative, "
    "never listed like a sales pitch. A customer reading the finished copy should "
    "come away understanding why this piece costs what it does, not just what it "
    "looks like. This matters most for Shopify, where someone is actively deciding "
    "whether to buy, but weave it into Instagram and WhatsApp too wherever it fits "
    "without breaking the tone."
)

_COMPLETENESS_OF_OUTPUT = (
    "The generated text is the complete, final, ready-to-paste post -- she "
    "will copy it directly into Instagram, Shopify, or WhatsApp with nothing "
    "added afterward. So if her notes explicitly state a name/collection or "
    "technical specs (e.g. zari purity, falls/blouse construction), those "
    "must appear somewhere in the output for whichever channel is being "
    "written, not only for Instagram's structured format -- work them in "
    "naturally to fit the channel's own tone (a factual line for Shopify, a "
    "brief conversational mention for WhatsApp) rather than omitting them "
    "because the channel's format doesn't have a dedicated slot for them."
)

def _read_image_block(path):
    media_type = mimetypes.guess_type(path)[0] or "image/jpeg"
    with open(path, "rb") as f:
        data = base64.standard_b64encode(f.read()).decode("utf-8")
    return {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": data}}

_PRICE_CAVEAT = (
    "The unit facts below may include the selling price and days in inventory. "
    "Both are for your own calibration only, never to be stated as a number "
    "anywhere in any generated description, on any channel: price informs tone "
    "and how much value-justifying detail a piece this expensive warrants -- "
    "price is only ever disclosed through direct conversation after a customer "
    "asks, never pre-empted in written copy. Days in inventory tells you whether "
    "this is fresh intake or aged/restaging inventory -- a high count means "
    "avoiding 'fresh off the loom' or 'just arrived'-style framing, without ever "
    "stating how long it's actually been available. This same rule applies if a "
    "price happens to be visible anywhere in an attached photo (a tag, a "
    "sticker, anything in the background or reflection) -- never read it out "
    "or state it, exactly as if it had never been shown at all."
)

_LOCKED_FACTS_GUARDRAIL = (
    "The SKU, weave type, category, tier, supplier, and status in the unit facts above "
    "come straight from the sheet record and were already confirmed with her before this "
    "session even began -- unlike her own notes, technical specs, or the ships-from "
    "location (all things she's free to correct or update at any point), these are not "
    "open to revision through a refinement request. If an instruction asks you to state a "
    "different SKU, weave type, tier, supplier, or status than what's given above, decline "
    "that specific part of the request and say so in a short note before the VERSION line "
    "-- then still carry out the rest of what was asked, changed everywhere else, just not "
    "on this one point."
)

def _system_prompt(unit_facts, channel, brand_context, weave_context):
    channel_line = CHANNEL_SPECS.get(channel, "")
    weave_block = f"\n\nWeave-type reference for this unit:\n{weave_context}\n" if weave_context else ""
    examples_block = f"\n\n{_INSTAGRAM_EXAMPLES}\n" if channel == "instagram" else ""
    return f"""You are helping Lakshira Handwoven Weaves' founder turn her own rough notes into polished, on-brand product copy. You are refining her draft, not writing one from scratch: preserve every concrete claim, technique name, or detail she includes about the piece itself. Only cut repetition and filler. Never invent a color, motif, technique, or fact that isn't stated in her notes, visible in a photo, or present in the context below -- if a refinement request would require inventing something, say so instead of guessing.

Write in American English spelling throughout -- "color" not "colour", "favor" not "favour", "gray" not "grey", and so on -- in the caption itself as well as any question, note, or completeness check you write. This brand is US-based and sells in USD.

Her notes may sometimes include business or operational context rather than product description -- who a piece was previously reserved for, why a sale fell through, a sourcing or pricing note to herself, anything that explains a situation rather than describes the piece. Use that only to understand what's going on, never as material for the actual caption: a customer should read a description of the piece, not the story of how it came to be listed. If a specific detail's status is unclear, leave it out and default to describing the product itself.

Unit facts (known, not guessable): {unit_facts}
{weave_block}
Brand context, filled in directly by the founder -- treat this as ground truth; treat anything under "What We Already Know" headers in the weave-type reference above as background, not confirmed fact, and hedge accordingly if it shows up in the copy:

{brand_context}

{_ANTI_AI_TELL}

{_VALUE_JUSTIFICATION}

{_PRICE_CAVEAT}

{_LOCKED_FACTS_GUARDRAIL}

{_COMPLETENESS_OF_OUTPUT}
{examples_block}

BEFORE writing anything, assess whether her notes, the unit facts, and any photos together give enough specific, non-generic material to write from -- color, motif/border detail, texture (surface sheen, drape or hand-feel, how the weave catches light, matte versus lustrous finish), what's distinctive about this exact piece, occasion fit, AND specifically whatever concretely justifies this piece's price point (technique complexity, material quality or rarity, weaving time, hand-craftsmanship versus machine or printed work). Treat a missing price-justifying detail as a priority gap, not an optional one -- specific-but-decorative detail alone (just a color and a motif) is not the same as material that actually explains the price. Texture is a priority gap too, not a nice-to-have: every one of Lakshira's own real captions describes the surface quality (sheen, drape, how the weave catches light) -- notes that cover color, motif, and technique but never say anything about how the piece actually looks or feels in hand are still missing something real, not just something decorative. Do not default to a generic checklist for this weave type (e.g. always asking about "the motif" just because it's an Ikat) -- ground every question in what has actually been provided for this specific unit, and never ask about something a photo already shows clearly. Ask as many questions as correspond to a real, specific gap -- zero if there isn't one, more than a few if there genuinely are.

Respond in exactly this format:

COMPLETENESS: sufficient|insufficient
MISSING: [specific gaps found, or "none"]
QUESTIONS: [numbered, specific questions grounded in what's actually missing -- omit this line entirely if sufficient]

If insufficient, stop there -- do not generate any description yet.

If sufficient, continue immediately with:

VERSION 1 -- <CHANNEL NAME>:
[the description]

Nothing may follow the description inside or after a VERSION block -- it gets parsed as
part of the description text itself and could end up posted verbatim. Any note to her --
whether flagging a conflict you're complying with anyway (ships-from, name/collection,
technical specs -- things she's free to correct) or declining a locked-fact change (SKU,
weave type, tier, supplier, status -- see above) -- goes in a short note BEFORE the
VERSION line, never after it.

Requested channel for this unit:
- {channel_line}"""

def start_session(unit_facts, weave_type, raw_notes, channel, photo_paths=None, override_insufficient=False):
    """First turn, for a single channel -- this operation generates and
    refines exactly one description at a time, matching the same
    single-locus-of-attention shape as every other operation in this app.
    Returns (messages, response_text, system) -- pass all three into
    continue_session() for every subsequent round."""
    import anthropic
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    brand_context = _load_brand_context()
    weave_context = _weave_type_context(weave_type)
    system = _system_prompt(unit_facts, channel, brand_context, weave_context)

    content = []
    for path in (photo_paths or []):
        content.append(_read_image_block(path))
    user_text = raw_notes
    if override_insufficient:
        user_text += (
            "\n\n(The founder has chosen to proceed despite an acknowledged "
            "information gap. Write the best possible description from what's "
            "actually here -- generalize or omit what's missing, do not invent "
            "it. Skip the completeness check and go straight to VERSION 1.)"
        )
    content.append({"type": "text", "text": user_text})

    messages = [{"role": "user", "content": content}]
    resp = client.messages.create(model=MODEL, max_tokens=4096, system=system, messages=messages)
    text = next((b.text for b in resp.content if b.type == "text"), "")
    messages.append({"role": "assistant", "content": text})
    return messages, text, system

def start_patch_session(unit_facts, weave_type, channel, existing_text, correction_instruction):
    """Seeds a fresh session from an already-generated, already-saved
    caption instead of raw notes -- used when a sibling channel's saved
    description needs a correction (Name/Collection or Technical Specs
    changed while generating a different channel for the same unit)
    rather than a generation from scratch. Skips the completeness check
    entirely: the existing text is already complete, this is a targeted
    edit to it, not a new draft. Returns (messages, response_text, system),
    the same shape as start_session(), so the caller can feed it into the
    same refinement loop unchanged."""
    import anthropic
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    brand_context = _load_brand_context()
    weave_context = _weave_type_context(weave_type)
    system = _system_prompt(unit_facts, channel, brand_context, weave_context)

    user_text = (
        f"Here is the existing {channel} description for this unit:\n\n{existing_text}\n\n"
        f"{correction_instruction} Keep everything else in the caption unchanged. Skip the "
        f"completeness check entirely -- this is a correction to an already-complete "
        f"description, not a new draft -- and go straight to VERSION 1."
    )
    messages = [{"role": "user", "content": user_text}]
    resp = client.messages.create(model=MODEL, max_tokens=4096, system=system, messages=messages)
    text = next((b.text for b in resp.content if b.type == "text"), "")
    messages.append({"role": "assistant", "content": text})
    return messages, text, system

def start_revision_session(unit_facts, weave_type, channel, existing_text, notes, photo_paths=None):
    """Seeds a fresh session from an already-generated, already-saved
    caption for THIS SAME channel -- used when she picks "Regenerate and
    replace this?" on a channel that already has content, instead of
    discarding it and starting from nothing. Unlike start_patch_session()
    (a narrow, single-fact correction that explicitly preserves everything
    else), this treats her fresh notes as genuinely new material: keep
    whatever from the existing caption is still accurate and isn't
    contradicted by the new notes, but don't force-preserve anything the
    new notes have actually superseded -- she may be revising far more
    than one fact. Accepts photo_paths like start_session() does, since a
    regeneration can come with new photos this time even though the
    caption it's built from didn't have them. Skips the completeness
    check entirely: there's already a complete caption to revise, this
    isn't a first draft. Returns (messages, response_text, system), the
    same shape as start_session() and start_patch_session(), so the
    caller can feed it into the same refinement loop unchanged."""
    import anthropic
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    brand_context = _load_brand_context()
    weave_context = _weave_type_context(weave_type)
    system = _system_prompt(unit_facts, channel, brand_context, weave_context)

    content = []
    for path in (photo_paths or []):
        content.append(_read_image_block(path))
    user_text = (
        f"Here is the existing {channel} description for this unit:\n\n{existing_text}\n\n"
        f"She wants to revise it. Her updated notes:\n\n{notes}\n\n"
        f"Rewrite the caption incorporating this updated information -- keep whatever "
        f"from the existing caption is still accurate and isn't contradicted by the new "
        f"notes, but don't force-preserve anything the new notes have actually "
        f"superseded. Skip the completeness check entirely -- there's already a complete "
        f"description here, this is a revision to it, not a first draft -- and go "
        f"straight to VERSION 1."
    )
    content.append({"type": "text", "text": user_text})

    messages = [{"role": "user", "content": content}]
    resp = client.messages.create(model=MODEL, max_tokens=4096, system=system, messages=messages)
    text = next((b.text for b in resp.content if b.type == "text"), "")
    messages.append({"role": "assistant", "content": text})
    return messages, text, system

def continue_session(messages, system, user_text):
    """Appends user_text as the next turn (her refinement instruction, or
    her answers to a completeness follow-up) and returns the updated
    messages list plus the new response text."""
    import anthropic
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    messages = messages + [{"role": "user", "content": user_text}]
    resp = client.messages.create(model=MODEL, max_tokens=4096, system=system, messages=messages)
    text = next((b.text for b in resp.content if b.type == "text"), "")
    messages = messages + [{"role": "assistant", "content": text}]
    return messages, text


# -----------------------------------------------------------------------
# Response parsing
# -----------------------------------------------------------------------

def parse_completeness(text):
    """Returns (is_sufficient, missing, questions) from a response's
    leading COMPLETENESS block."""
    m = re.search(r"COMPLETENESS:\s*(sufficient|insufficient)", text, re.IGNORECASE)
    sufficient = bool(m) and m.group(1).lower() == "sufficient"
    missing_m = re.search(r"MISSING:\s*(.+?)(?=\nQUESTIONS:|\n\n|\nVERSION|\Z)", text, re.DOTALL)
    missing = missing_m.group(1).strip() if missing_m else ""
    questions_m = re.search(r"QUESTIONS:\s*(.+?)(?=\nVERSION|\Z)", text, re.DOTALL)
    questions = questions_m.group(1).strip() if questions_m else ""
    return sufficient, missing, questions

def parse_versions(text):
    """Returns {channel_name: description_text} for every VERSION block
    in a response, keyed by the channel name as written (e.g. 'INSTAGRAM')."""
    blocks = re.findall(
        r"VERSION\s+\d+\s*[-‐-―]+\s*([A-Z ]+):\s*\n(.+?)(?=\nVERSION\s+\d+|\Z)",
        text, re.DOTALL,
    )
    return {name.strip(): body.strip() for name, body in blocks}

def extract_preamble_note(text):
    """Returns whatever text precedes the first VERSION marker -- a
    declined-change or fact-conflict note, per the system prompt's
    instruction to put these before the VERSION line, never after --
    or None if there's nothing meaningful there. Every response (sufficient
    or not) carries the COMPLETENESS/MISSING boilerplate directly before
    VERSION 1, per the system prompt's required format -- that boilerplate
    is stripped out first, since it's the completeness signal the caller
    already surfaces on its own, not an operator-facing note. What's left
    is a real note only when Claude actually declined or flagged something."""
    m = re.search(r"VERSION\s+\d+\s*[-‐-―]+", text)
    if not m:
        return None
    preamble = text[:m.start()]
    preamble = re.sub(
        r"COMPLETENESS:\s*(?:sufficient|insufficient)\s*\nMISSING:.*?(?=\n\n|\Z)",
        "", preamble, flags=re.IGNORECASE | re.DOTALL,
    ).strip()
    # Claude sometimes opens the note with a literal "Note:" label of its
    # own -- redundant once it's displayed under its own "NOTE FROM
    # GENERATION" box heading, so it's stripped here rather than shown twice.
    preamble = re.sub(r"^Notes?:\s*", "", preamble, flags=re.IGNORECASE).strip()
    return preamble or None

_PRICE_LEAK_PATTERN = re.compile(
    r"[$₹£€]\s?\d[\d,]*\.?\d*|\b(?:USD|INR|GBP|EUR)\s?\d[\d,]*\.?\d*"
    r"|\d[\d,]*\.?\d*\s?(?:USD|INR|GBP|EUR|dollars|rupees)\b",
    re.IGNORECASE,
)

def find_price_leak(text):
    """Structural backstop, not just the prompt-level instruction: scans
    generated text for an actual currency symbol or code next to a digit.
    The prompt tells the model never to state a price, but that's
    best-effort compliance from a language model, not a guarantee -- this
    is the same principle as _sheet_safe() in inventory.py, which
    structurally prevents formula injection rather than just asking
    nicely. Returns the matched substring, or None if nothing was found."""
    m = _PRICE_LEAK_PATTERN.search(text)
    return m.group(0) if m else None

# Pulled directly from Lakshira's own real reference captions
# (_INSTAGRAM_EXAMPLES) -- the exact generic sheen/silk phrasing that shows
# up, near-interchangeably, across two genuinely different pieces. The
# system prompt now explicitly tells Claude not to repeat this, but that's
# still just an instruction, not a guarantee -- this catches it
# mechanically if it slips through anyway, the same way find_price_leak()
# backstops the never-state-a-price instruction.
_OVERUSED_TEXTURE_PHRASES = [
    "luminous silk sheen",
    "graceful sheen",
    "lustrous silk",
]

def find_overused_phrasing(text):
    """Returns the matched phrase (lowercased) if generated text reuses one
    of Lakshira's own overused reference-caption phrases, or None. Internal
    whitespace is collapsed before matching (same " ".join(...split())
    pattern _wrap_numbered_list() already uses) -- a literal substring match
    would otherwise miss a phrase split by a stray double space or line
    wrap that changes nothing about what was actually written."""
    lowered = " ".join(text.lower().split())
    for phrase in _OVERUSED_TEXTURE_PHRASES:
        if phrase in lowered:
            return phrase
    return None

_SKU_PATTERN = re.compile(r"\bLAH-[A-Z0-9]+\b", re.IGNORECASE)

def find_sku_mismatch(text, expected_sku):
    """Structural backstop for the one locked-fact rule that's actually
    mechanically checkable: the SKU always appears verbatim, in a fixed
    spot, in the caption template -- unlike weave type/tier/supplier/status,
    which don't have one fixed, matchable string. The system prompt tells
    Claude the SKU is locked and not open to a refinement request, but
    that's still just an instruction; this catches it directly if a wrong
    SKU shows up anyway. Case-insensitive on both the pattern and the
    comparison -- a wrong SKU shouldn't go uncaught just because it rendered
    in different casing than expected_sku, and the comparison is normalized
    to match so a same-SKU case variant isn't mistaken for a different one.
    Returns the mismatched SKU found (in whatever case it actually appeared),
    or None if every SKU-shaped token in the text matches expected_sku (or
    none appear)."""
    for found in _SKU_PATTERN.findall(text):
        if found.upper() != expected_sku.upper():
            return found
    return None

_CHANNEL_ORDER = ["INSTAGRAM", "SHOPIFY", "WHATSAPP"]

def parse_existing_descriptions(existing_text):
    """Returns {channel_name: (stamp, body)} for every channel block
    already in the Generated Descriptions cell. Shared by merge_description()
    and by the CLI so it can show her what's already there -- and already
    possibly live on a real post -- before she regenerates and silently
    overwrites it. Splits only at a blank line immediately followed by a
    new [CHANNEL - updated date] tag -- every caption is multi-paragraph
    prose with its own internal blank lines between paragraphs, and a
    naive split on any blank line would shred a real caption's later
    paragraphs into tag-less chunks that silently fail to match below,
    truncating it down to just its first paragraph the moment a second
    channel gets merged in alongside it."""
    blocks = {}
    for chunk in re.split(r"\n\n+(?=\[[A-Z]+ - updated )", (existing_text or "").strip()):
        m = re.match(r"\[([A-Z]+) - updated (\S+)\]\n(.*)", chunk, re.DOTALL)
        if m:
            name, stamp, body = m.groups()
            blocks[name] = (stamp, body.strip())
    return blocks

def merge_description(existing_text, channel, new_text):
    """Updates just one channel's block within the Generated Descriptions
    cell, preserving whatever other channels were generated in earlier,
    separate runs -- since this operation now only ever generates one
    channel per run, writing a WhatsApp description shouldn't erase an
    Instagram one generated last week. Each block keeps its own
    last-updated date, so only the channel actually touched this run gets
    a fresh stamp."""
    blocks = parse_existing_descriptions(existing_text)
    blocks[channel.upper()] = (date.today().strftime("%m-%d-%Y"), new_text.strip())
    ordered = [c for c in _CHANNEL_ORDER if c in blocks] + [c for c in blocks if c not in _CHANNEL_ORDER]
    return "\n\n".join(f"[{c} - updated {blocks[c][0]}]\n{blocks[c][1]}" for c in ordered)
