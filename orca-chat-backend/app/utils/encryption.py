import base64


def encrypt_message(content: str) -> str:
    return base64.b64encode(content.encode("utf-8")).decode("ascii")


def decrypt_message(content: str) -> str:
    return base64.b64decode(content.encode("ascii")).decode("utf-8")
