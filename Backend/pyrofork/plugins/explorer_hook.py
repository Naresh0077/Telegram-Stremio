from pyrogram import Client, filters
from pyrogram.types import Message

from Backend.helper.explorer import configured_channels, get_store, message_to_doc
from Backend.logger import LOGGER


#----- Keep the File Explorer index live: every attachment posted to a configured channel is indexed.
#----- Runs in its own handler group, so the existing receiver (movie/TV indexing) is unaffected.
@Client.on_message(filters.channel & filters.media, group=50)
async def explorer_index_new(client: Client, message: Message):
    try:
        if message.chat.id not in configured_channels():
            return
        doc = message_to_doc(message, getattr(message.chat, "title", "") or "")
        if doc:
            await get_store().upsert_doc(doc)
    except Exception as e:
        LOGGER.warning(f"[Explorer] live index failed for message {getattr(message, 'id', '?')}: {e}")


@Client.on_edited_message(filters.channel & filters.media, group=50)
async def explorer_index_edited(client: Client, message: Message):
    await explorer_index_new(client, message)


@Client.on_deleted_messages(filters.channel, group=50)
async def explorer_index_deleted(client: Client, messages: list[Message]):
    try:
        allowed = configured_channels()
        by_chat: dict = {}
        for m in messages:
            if m.chat and m.chat.id in allowed:
                by_chat.setdefault(m.chat.id, []).append(m.id)
        for chat_id, ids in by_chat.items():
            await get_store().delete_messages(chat_id, ids)
    except Exception as e:
        LOGGER.warning(f"[Explorer] live delete failed: {e}")
