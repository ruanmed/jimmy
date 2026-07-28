"""
Convert Telegram chats to the intermediate format.

Based on Telegram Data Export Schema: <https://core.telegram.org/import-export>
"""

from collections import defaultdict
from datetime import datetime
import json
from pathlib import Path

from jimmy import common, converter, intermediate_format as imf
import jimmy.md_lib.conversations
import jimmy.md_lib.links


class Converter(converter.BaseConverter):
    def _convert_text_entities(self, text_entities: list) -> str:
        """Convert Telegram text_entities to Markdown."""

        if not text_entities:
            return ""

        parts = []

        for entity in text_entities:
            text = entity.get("text", "")
            etype = entity.get("type", "plain")

            # ---- Basic formatting ----
            if etype == "bold":
                text = f"**{text}**"
            elif etype == "italic":
                text = f"*{text}*"
            elif etype == "underline":
                text = f"__{text}__"  # or use <u> if Markdown not supported, but Jimmy handles __
            elif etype == "strikethrough":
                text = f"~~{text}~~"
            elif etype == "code":
                text = f"`{text}`"
            elif etype == "pre":
                # code block – optionally preserve language if provided (not in schema)
                text = f"\n```\n{text}\n```\n"

            # ---- Links and mentions ----
            elif etype == "text_link":
                url = entity.get("url", "")
                text = f"[{text}]({url})"
            elif etype == "text_mention":
                user_id = entity.get("user_id", "")
                text = f"[{text}](tg://user?id={user_id})"

            # ---- Plain or already‐self‑descriptive types ----
            # mention, hashtag, bot_command, url, email, etc. are kept as plain text
            # because they are human‑readable markers.

            parts.append(text)

        return "".join(parts)

    def _handle_media(
        self,
        message: dict,
        include_thumbnail: bool = True,
        inline_thumbnail: bool = True,
        use_image_thumbnails: bool = False,
    ) -> tuple[str, list[imf.Resource]]:
        """
        Detect media in the message and return (media_markdown, resources).

        Parameters:
            include_thumbnail (bool):
                If True, the thumbnail (if present) will be added as a separate
                Resource object. The file will be copied to the output folder.
            inline_thumbnail (bool):
                If True and a thumbnail is used as the link text, the thumbnail
                Resource will have its `original_text` set so the writer replaces
                the inline image placeholder. If False, the thumbnail is only
                attached as a resource without being embedded in the note body.
            use_image_thumbnails (bool):
                If True, thumbnails will also be used for image media. Normally,
                images are displayed directly; with this flag, the thumbnail
                becomes a clickable link to the full‑size image.

        Returns:
            (media_markdown, resources)
            - media_markdown: the Markdown string to insert into the note body.
            - resources: list of Resource objects to be written to disk.
        """
        # Order of precedence – if multiple exist, use the first one
        media_fields = [
            "photo",
            "video",
            "voice",
            "audio",
            "video_message",
            "animation",
            "sticker",
            "file",
        ]

        media_path = None
        media_type = None
        for field in media_fields:
            if field in message and message[field]:
                media_path = message[field]
                media_type = field
                break

        if not media_path:
            return "", []

        # ----- Main media -----
        # Get file name (might not be present for some types)
        file_name = message.get("file_name", Path(media_path).name)

        # Locate the file (fallback to root_path / media_path)
        resource_path = common.find_file_recursively(self.root_path, media_path) or (
            self.root_path / media_path
        )

        # Determine if it's an image (based on type or extension)
        is_image = media_type in ["photo", "sticker"] or common.is_image(resource_path)
        has_thumbnail = "thumbnail" in message and message["thumbnail"]

        resources = []

        # 1. Decide whether to include the thumbnail as a resource
        include_thumb_resource = (
            include_thumbnail and has_thumbnail and (not is_image or use_image_thumbnails)
        )

        # 2. Decide whether to use the thumbnail as the link text in the main marker
        #    This requires that we want to inline it (so the placeholder will be replaced)
        use_thumbnail_for_marker = include_thumb_resource and inline_thumbnail

        # 3. Create the thumbnail resource if requested
        thumb_marker = None
        if include_thumb_resource:
            thumb_path = message["thumbnail"]
            thumb_resource_path = common.find_file_recursively(self.root_path, thumb_path) or (
                self.root_path / thumb_path
            )
            thumb_marker = f"![Thumbnail]({thumb_resource_path})"
            thumb_name = f"thumbnail_{Path(thumb_path).name}"

            thumb_resource = imf.Resource(
                filename=thumb_resource_path,
                original_text=thumb_marker if inline_thumbnail else None,
                title=thumb_name,
            )

            resources.append(thumb_resource)

        # 4. Build the main media marker
        if is_image and not use_thumbnail_for_marker:
            # Display the image directly
            main_marker = f"![{file_name}]({resource_path})"
            main_title = file_name
        elif use_thumbnail_for_marker:
            # Use the thumbnail as a clickable link to the main media
            main_marker = f"[{thumb_marker}]({resource_path})"
            main_title = thumb_marker
        else:
            # Plain file link (no thumbnail inline)
            main_marker = f"[{file_name}]({resource_path})"
            main_title = file_name

        # 5. Create the main media resource
        main_resource = imf.Resource(
            filename=resource_path,
            original_text=main_marker,
            title=main_title,
        )

        # Insert main resource first (so it's processed before the thumbnail)
        resources.insert(0, main_resource)

        return main_marker, resources

    def _get_message_text(self, message: dict) -> str:
        """Extract plain text from a Telegram message, handling entity lists."""

        text = message.get("text", "")

        if isinstance(text, list):
            # text is a list of entities: [{"text": "Hello", "type": "plain"}, ...]
            return "".join(part.get("text", "") for part in text if isinstance(part, dict))

        return text if isinstance(text, str) else ""

    def _process_message(
        self, message: dict, dt: datetime
    ) -> tuple[str, list[imf.Resource], list[str]]:
        """Process a single message, returning (body_line, resources, tags)."""

        # Get the rich text if available
        if "text_entities" in message:
            content = self._convert_text_entities(message["text_entities"])
        else:
            # Fallback to plain text (for older exports or simple messages)
            content = message.get("text", "")

        # Handle media
        media_markdown, media_resources = self._handle_media(message)

        # Combine text and media (choose your preferred formatting)
        if content and media_markdown:
            # media first, text as caption (blockquote)
            caption = "\n".join(f"> {line}" for line in content.splitlines())
            full_content = f"{media_markdown}\n\n{caption}"
        elif media_markdown:
            full_content = media_markdown
        else:
            full_content = content

        # Extract tags from the original text (before media markdown)
        tags = jimmy.md_lib.tags.get_inline_tags(content, ["#"])

        sender = message.get("from", "Unknown")
        time_str = dt.strftime("%H:%M:%S")
        body_line = f"{time_str}, **{sender}**: {full_content}"

        return body_line, media_resources, tags

    def _build_note_from_messages(
        self,
        messages: list[tuple[datetime, dict]],
        title: str,
        original_id: str,
        chat_id: int,
        extra_frontmatter: dict | None = None,
    ) -> imf.Note | None:
        """Create a Note from a sorted list of (datetime, message) tuples."""

        if not messages:
            return None

        first_date = messages[0][0]
        last_date = messages[-1][0]

        note = imf.Note(title, source_application=self.format, original_id=original_id)
        note.created = first_date
        note.updated = last_date

        body_lines = []
        resources = []
        all_tags = []

        for dt, message in messages:
            line, res, tags = self._process_message(message, dt)
            body_lines.append(line)
            resources.extend(res)
            all_tags.extend(tags)

        note.body = "\n\n".join(body_lines)
        note.resources = resources
        note.tags = [imf.Tag(tag) for tag in dict.fromkeys(all_tags)]

        frontmatter = {
            "chat_id": chat_id,
            "message_count": len(messages),
            "created": first_date.isoformat(),
            "updated": last_date.isoformat(),
        }

        if extra_frontmatter:
            frontmatter.update(extra_frontmatter)

        note.frontmatter = frontmatter

        return note

    @common.catch_all_exceptions()
    def convert_note(self, chat):
        if chat.get("type", "") == "saved_messages":
            title = "Saved Messages"
        else:
            title = chat.get("name", "Unnamed Chat")
        self.logger.debug(f'Converting chat "{title}"')

        messages = chat.get("messages", [])

        # Filter out non‑message events and create list of (dt, msg)
        msg_tuples = []

        for message in messages:
            if message.get("type") != "message":
                continue

            dt = common.timestamp_to_datetime(int(message["date_unixtime"]))
            msg_tuples.append((dt, message))

        if not msg_tuples:
            self.logger.debug(f"No regular messages in chat '{title}', skipping.")

            return

        # Sort by datetime (should already be in order, but safe)
        msg_tuples.sort(key=lambda x: x[0])

        note = self._build_note_from_messages(
            msg_tuples,
            title=title,
            original_id=str(chat["id"]),
            chat_id=chat.get("id"),
            extra_frontmatter={"chat_type": chat.get("type")},
        )

        # Handle creation time from a service message if needed
        # (you can add that logic before building or inside)
        if note is not None:
            self.root_notebook.child_notes.append(note)

    def convert_saved_messages_grouped_by_day(self, chat):
        if chat.get("type") != "saved_messages":
            self.logger.warning("This method is intended for 'saved_messages' chats only.")
            self.convert_note(chat)

            return

        messages_by_day = defaultdict(list)

        for message in chat.get("messages", []):
            if message.get("type") != "message":
                continue

            dt = common.timestamp_to_datetime(int(message["date_unixtime"]))
            messages_by_day[dt.date()].append((dt, message))

        if not messages_by_day:
            self.logger.debug("No regular messages found in saved messages chat.")

            return

        for day, msg_list in messages_by_day.items():
            msg_list.sort(key=lambda x: x[0])  # already sorted, but ensure
            title = f"Saved Messages – {day.isoformat()}"

            note = self._build_note_from_messages(
                msg_list,
                title=title,
                original_id=f"{chat['id']}_{day.isoformat()}",
                chat_id=chat.get("id"),
                extra_frontmatter={"date": day.isoformat()},
            )

            if note:
                self.root_notebook.child_notes.append(note)
                self.logger.debug(f"Created note for {day}")

    def convert(self, file_or_folder: Path):
        input_json = json.loads((file_or_folder / "result.json").read_text(encoding="utf-8"))

        if (chats := input_json.get("chats")) is not None:
            self.logger.debug('Found "chats" key. Assuming that this is a complete "DataExport".')

            for chat in chats["list"]:
                # Dispatch: if it's Saved Messages, group by day; otherwise use normal conversion
                if chat.get("type") == "saved_messages":
                    self.convert_saved_messages_grouped_by_day(chat)
                else:
                    self.convert_note(chat)
        else:
            self.logger.debug('No "chats" key. Assuming that this is a single "ChatExport".')

            # For a single export, check if it's Saved Messages
            if input_json.get("type") == "saved_messages":
                self.convert_saved_messages_grouped_by_day(input_json)
            else:
                self.convert_note(input_json)
