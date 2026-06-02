import os
import json
import lark_oapi as lark
from lark_oapi.api.im.v1 import *


APP_ID = os.getenv("FEISHU_APP_ID")
APP_SECRET = os.getenv("FEISHU_APP_SECRET")

if not APP_ID or not APP_SECRET:
    raise RuntimeError("请先设置环境变量 FEISHU_APP_ID 和 FEISHU_APP_SECRET")


def do_p2_im_message_receive_v1(data: lark.im.v1.P2ImMessageReceiveV1) -> None:
    """
    处理飞书接收消息事件：im.message.receive_v1
    """
    print("\n收到飞书消息事件：")
    print(lark.JSON.marshal(data, indent=4))

    try:
        event = data.event
        message = event.message

        chat_id = message.chat_id
        message_type = message.message_type
        content = message.content

        print("chat_id:", chat_id)
        print("message_type:", message_type)
        print("content:", content)

    except Exception as e:
        print("解析消息失败：", e)


event_handler = (
    lark.EventDispatcherHandler.builder("", "")
    .register_p2_im_message_receive_v1(do_p2_im_message_receive_v1)
    .build()
)


def main():
    cli = lark.ws.Client(
        APP_ID,
        APP_SECRET,
        event_handler=event_handler,
        log_level=lark.LogLevel.DEBUG,
    )

    print("正在启动飞书长连接客户端...")
    cli.start()


if __name__ == "__main__":
    main()