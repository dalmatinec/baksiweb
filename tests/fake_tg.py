"""Минимальный фейковый Bot API для тестов модулей: запоминает отправленное, отдаёт правдоподобные ответы."""
from __future__ import annotations

import itertools
import time
from dataclasses import dataclass, field
from typing import Any

from aiogram import Bot
from aiogram.client.session.base import BaseSession
from aiogram.exceptions import TelegramForbiddenError
from aiogram.methods import (AnswerCallbackQuery, CloseForumTopic, CopyMessage, CreateForumTopic, DeleteMessage,
                             DeleteWebhook, EditMessageReplyMarkup, GetChat, SetMyCommands, SendDocument, EditMessageText, ForwardMessage, GetChatMember,
                             GetMe, SendMessage, SetMessageReaction, TelegramMethod)
from aiogram.types import (CallbackQuery, Chat, ChatFullInfo, ChatMemberAdministrator, ChatMemberMember, ForumTopic, Message,
                           MessageId, Update, User)

BOT = User(id=999, is_bot=True, first_name="TestBot", username="test_bot")


@dataclass
class Sent:
    method: str
    chat_id: int
    message_id: int
    text: str | None = None
    from_chat: int | None = None
    source_id: int | None = None
    markup: Any = None
    thread: int | None = None
    reply_to: int | None = None


@dataclass
class FakeSession(BaseSession):
    sent: list[Sent] = field(default_factory=list)
    blocked: set[int] = field(default_factory=set)
    admins: set[tuple[int, int]] = field(default_factory=set)
    reactions: list[tuple[int, int]] = field(default_factory=list)
    answers: list[str] = field(default_factory=list)
    forums: set[int] = field(default_factory=set)
    commands: dict = field(default_factory=dict)
    reaction_emoji: dict = field(default_factory=dict)
    documents: list = field(default_factory=list)

    def __post_init__(self) -> None:
        BaseSession.__init__(self)
        self._ids = itertools.count(5000)

    async def close(self) -> None:
        pass

    async def stream_content(self, *a, **k):  # pragma: no cover
        yield b""

    def _msg(self, chat_id: int, mid: int, text: str | None = None, thread: int | None = None) -> Message:
        return self._raw(chat_id, mid, text, thread).as_(self.bot)

    def _raw(self, chat_id: int, mid: int, text: str | None = None, thread: int | None = None) -> Message:
        return Message(message_id=mid, date=int(time.time()), chat=Chat(id=chat_id, type="private" if chat_id > 0
                                                                       else "supergroup"),
                       from_user=BOT, text=text, message_thread_id=thread)

    async def make_request(self, bot: Bot, method: TelegramMethod, timeout: int | None = None) -> Any:
        self.bot = bot
        chat_id = getattr(method, "chat_id", None)
        if chat_id in self.blocked:
            raise TelegramForbiddenError(method=method, message="Forbidden: bot was blocked by the user")
        if isinstance(method, GetMe):
            return BOT
        if isinstance(method, DeleteWebhook):
            return True
        if isinstance(method, SendMessage):
            mid = next(self._ids)
            self.sent.append(Sent("send", chat_id, mid, method.text, markup=method.reply_markup,
                                  thread=method.message_thread_id))
            return self._msg(chat_id, mid, method.text, method.message_thread_id)
        if isinstance(method, ForwardMessage):
            mid = next(self._ids)
            self.sent.append(Sent("forward", chat_id, mid, from_chat=method.from_chat_id, source_id=method.message_id))
            return self._msg(chat_id, mid)
        if isinstance(method, CopyMessage):
            mid = next(self._ids)
            reply = method.reply_parameters.message_id if method.reply_parameters else None
            self.sent.append(Sent("copy", chat_id, mid, from_chat=method.from_chat_id, source_id=method.message_id,
                                  markup=method.reply_markup, thread=method.message_thread_id, reply_to=reply))
            return MessageId(message_id=mid)
        if isinstance(method, EditMessageText):
            self.sent.append(Sent("edit", chat_id, method.message_id, method.text, markup=method.reply_markup))
            return self._msg(chat_id, method.message_id, method.text)
        if isinstance(method, (EditMessageReplyMarkup,)):
            return True
        if isinstance(method, AnswerCallbackQuery):
            self.answers.append(method.text or "")
            return True
        if isinstance(method, SetMessageReaction):
            self.reactions.append((chat_id, method.message_id))
            self.reaction_emoji[(chat_id, method.message_id)] = [r.emoji for r in method.reaction or []]
            return True
        if isinstance(method, SendDocument):
            mid = next(self._ids)
            name = getattr(method.document, "filename", None) or str(getattr(method.document, "path", ""))
            self.sent.append(Sent("document", chat_id, mid, method.caption, source_id=None, thread=None))
            self.documents.append((chat_id, name, method.caption))
            return self._msg(chat_id, mid)
        if isinstance(method, SetMyCommands):
            scope = getattr(method.scope, "chat_id", None)
            self.commands[scope] = [c.command for c in method.commands]
            return True
        if isinstance(method, GetChat):
            return ChatFullInfo.model_construct(id=chat_id, type="supergroup", is_forum=chat_id in self.forums)
        if isinstance(method, GetChatMember):
            if (chat_id, method.user_id) in self.admins:
                return ChatMemberAdministrator.model_construct(
                    status="administrator", user=User(id=method.user_id, is_bot=False, first_name="A"))
            return ChatMemberMember(user=User(id=method.user_id, is_bot=False, first_name="M"))
        if isinstance(method, DeleteMessage):
            return True
        if isinstance(method, CreateForumTopic):
            return ForumTopic(message_thread_id=next(self._ids), name=method.name, icon_color=0)
        if isinstance(method, CloseForumTopic):
            return True
        raise NotImplementedError(type(method).__name__)

    def to(self, chat_id: int, method: str | None = None) -> list[Sent]:
        return [s for s in self.sent if s.chat_id == chat_id and (method is None or s.method == method)]


_uid = itertools.count(1)


def user(uid: int, name: str = "User", username: str | None = None) -> User:
    return User(id=uid, is_bot=False, first_name=name, username=username)


def message_update(chat_id: int, from_user: User, text: str | None, mid: int | None = None,
                   reply_to: int | None = None, thread: int | None = None, chat_type: str | None = None,
                   entities: list | None = None) -> Update:
    chat = Chat(id=chat_id, type=chat_type or ("private" if chat_id > 0 else "supergroup"), title=None if chat_id > 0
                else "Group")
    reply = Message(message_id=reply_to, date=int(time.time()), chat=chat, from_user=BOT) if reply_to else None
    msg = Message(message_id=mid or next(_uid) + 100, date=int(time.time()), chat=chat, from_user=from_user,
                  text=text, reply_to_message=reply, message_thread_id=thread, entities=entities,
                  is_topic_message=True if thread else None)
    return Update(update_id=next(_uid), message=msg)


def callback_update(chat_id: int, from_user: User, data: str, message_id: int = 1) -> Update:
    chat = Chat(id=chat_id, type="private" if chat_id > 0 else "supergroup")
    msg = Message(message_id=message_id, date=int(time.time()), chat=chat, from_user=BOT, text="x")
    return Update(update_id=next(_uid), callback_query=CallbackQuery(id=str(next(_uid)), from_user=from_user,
                                                                      chat_instance="c", message=msg, data=data))
