from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.encoders import jsonable_encoder
from sqlalchemy import exists, or_, select
from sqlalchemy.orm import aliased
from sqlalchemy.orm import Session

from app.database import SessionLocal, get_db
from app.dependencies import get_current_user
from app.models import Conversation, ConversationMember, Message, User, UserBlock
from app.modules.chat import schemas, service
from app.modules.chat.websocket import manager
from app.utils.security import authenticate_access_token

router = APIRouter(tags=["chat"])


@router.get("/chats", response_model=list[schemas.ConversationListOut])
def chats(
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    other_member = aliased(ConversationMember)
    blocked_by_me = select(UserBlock.blocked_id).where(
        UserBlock.blocker_id == current_user.id,
        UserBlock.status == "active",
    )
    blocked_me = select(UserBlock.blocker_id).where(
        UserBlock.blocked_id == current_user.id,
        UserBlock.status == "active",
    )
    rows = (
        db.query(Conversation)
        .join(ConversationMember, ConversationMember.conversation_id == Conversation.id)
        .filter(
            ConversationMember.user_id == current_user.id,
            ~exists().where(
                other_member.conversation_id == Conversation.id,
                other_member.user_id != current_user.id,
                or_(
                    other_member.user_id.in_(blocked_by_me),
                    other_member.user_id.in_(blocked_me),
                ),
            ),
        )
        .order_by(Conversation.created_at.desc())
        .offset(offset)
        .limit(limit)
        .all()
    )
    return service.conversation_list_items(db, current_user, rows)


@router.post("/chats", response_model=schemas.ConversationOut)
def create_chat(payload: schemas.CreateConversationRequest, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    conversation = service.get_or_create_direct_conversation(db, current_user, payload.receiver_id)
    db.commit()
    db.refresh(conversation)
    return conversation


@router.get("/chats/{chat_id}", response_model=schemas.ConversationDetailOut)
def chat_detail(chat_id: UUID, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    return service.conversation_detail(db, current_user, chat_id)


@router.get("/chats/{chat_id}/messages", response_model=list[schemas.MessageOut])
def messages(
    chat_id: UUID,
    limit: int = Query(default=200, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    member = db.query(ConversationMember).filter_by(conversation_id=chat_id, user_id=current_user.id).first()
    if member is None:
        raise HTTPException(status_code=403, detail="Conversation access denied")
    rows = (
        db.query(Message)
        .filter(Message.conversation_id == chat_id)
        .order_by(Message.created_at.asc())
        .offset(offset)
        .limit(limit)
        .all()
    )
    return [service.message_out(row) for row in rows]


@router.post("/chats/{chat_id}/messages", response_model=schemas.MessageOut)
async def send_message(
    chat_id: UUID,
    payload: schemas.SendMessageRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    x_idempotency_key: str | None = Header(default=None),
):
    message = service.send_message(
        db,
        current_user,
        chat_id,
        payload.receiver_id,
        payload.content,
        x_idempotency_key,
        use_free_quota=payload.use_free_quota,
    )
    body = service.message_out(message)
    await manager.send_user(payload.receiver_id, {"type": "message.received", "payload": body})
    return body


@router.post("/chats/{chat_id}/read", response_model=schemas.ConversationReadResponse)
def mark_chat_read(chat_id: UUID, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    return service.mark_conversation_read(db, current_user, chat_id)


@router.post("/messages/{message_id}/read", response_model=schemas.ReadReceiptResponse)
def mark_read(message_id: UUID, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    message = service.mark_message_read(db, current_user, message_id)
    return {"status": "ok", "delivered_at": message.delivered_at, "read_at": message.read_at}


@router.post("/messages/{message_id}/delivered", response_model=schemas.ReadReceiptResponse)
def mark_delivered(message_id: UUID, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    message = service.mark_message_delivered(db, current_user, message_id)
    return {"status": "ok", "delivered_at": message.delivered_at, "read_at": message.read_at}


@router.post("/users/{user_id}/block")
def block_user(user_id: UUID, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    block = service.block_user(db, current_user, user_id)
    return {"blocked_id": block.blocked_id, "status": block.status}


@router.post("/users/{user_id}/unblock")
def unblock_user(user_id: UUID, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    return service.unblock_user(db, current_user, user_id)


@router.post("/messages/{message_id}/report")
def report_message(
    message_id: UUID,
    payload: schemas.ReportMessageRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    report = service.report_message(db, current_user, message_id, payload.reason, payload.description)
    return {"report_id": report.id, "status": report.status}


@router.websocket("/ws/chat")
async def chat_ws(websocket: WebSocket, token: str):
    with SessionLocal() as db:
        try:
            current_user = authenticate_access_token(token, db)
            user_id = current_user.id
        except HTTPException:
            await websocket.close(code=4401)
            return

    await manager.connect(user_id, websocket)
    try:
        while True:
            event = await websocket.receive_json()
            if event.get("type") == "message.send":
                data = event.get("payload", {})
                try:
                    chat_id = UUID(data["chat_id"])
                    receiver_id = UUID(data["receiver_id"])
                    content = data["content"]
                except (KeyError, TypeError, ValueError):
                    await websocket.send_json({
                        "type": "error",
                        "payload": {"detail": "Invalid message.send payload"},
                    })
                    continue
                if not isinstance(content, str) or not content.strip():
                    await websocket.send_json({
                        "type": "error",
                        "payload": {"detail": "Message content is required"},
                    })
                    continue

                with SessionLocal() as db:
                    try:
                        sender = authenticate_access_token(token, db)
                    except HTTPException:
                        await websocket.close(code=4401)
                        return
                    try:
                        message = service.send_message(
                            db,
                            sender,
                            chat_id,
                            receiver_id,
                            content,
                            data.get("idempotency_key"),
                            use_free_quota=bool(data.get("use_free_quota", False)),
                        )
                    except HTTPException as exc:
                        await websocket.send_json({"type": "error", "payload": {"detail": exc.detail}})
                        continue
                    body = service.message_out(message)
                await manager.send_user(receiver_id, {"type": "message.received", "payload": body})
                await websocket.send_json(jsonable_encoder({"type": "message.sent", "payload": body}))
    except WebSocketDisconnect:
        pass
    finally:
        manager.disconnect(user_id, websocket)
