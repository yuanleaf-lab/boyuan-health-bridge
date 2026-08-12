from __future__ import annotations

import asyncio
import contextlib
import hmac
import html
from dataclasses import dataclass

from mcp.server.auth.settings import AuthSettings
from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations
from mi_fitness import XiaomiAuth
from pydantic import AnyHttpUrl
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse
from starlette.routing import Mount, Route

from app.config import Settings
from app.health import HealthBridge, Metric
from app.oauth import BridgeOAuthProvider, SignedTokens

settings = Settings.from_env()
signer = SignedTokens(settings.signing_secret)
health_bridge = HealthBridge(settings.mi_token_json)
oauth_provider = BridgeOAuthProvider(settings.public_base_url, settings.mcp_url, signer)


@dataclass
class SetupState:
    status: str = "idle"
    qr_image_url: str = ""
    login_url: str = ""
    error: str = ""
    token_json: str = ""
    task: asyncio.Task[None] | None = None


setup_state = SetupState()

mcp = MCPServer(
    "柏渊健康桥",
    instructions=(
        "读取小米运动健康中已由亲友主动共享的数据。所有工具均为只读。"
        "健康数据可能有同步延迟，不能用于医疗诊断或紧急情况。"
    ),
    auth=AuthSettings(
        issuer_url=AnyHttpUrl(settings.public_base_url),
        resource_server_url=AnyHttpUrl(settings.mcp_url),
        required_scopes=["health.read"],
    ),
    auth_server_provider=oauth_provider,
)

READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True)


@mcp.tool(
    title="列出健康共享亲友",
    description="列出小米运动健康中已经向当前账号共享健康数据的亲友。",
    annotations=READ_ONLY,
)
async def list_family_members() -> dict:
    return {"members": await health_bridge.list_relatives()}


@mcp.tool(
    title="读取最近健康快照",
    description=(
        "读取一位亲友最近一次同步的健康快照、每日摘要和已共享的数据类型。"
        "relative 可填写亲友备注名或 UID；只有一位亲友时可以省略。"
    ),
    annotations=READ_ONLY,
)
async def get_health_snapshot(relative: str | None = None) -> dict:
    return await health_bridge.snapshot(relative)


@mcp.tool(
    title="读取健康历史",
    description=(
        "按日期读取亲友的健康历史。支持心率、睡眠、步数、卡路里、血氧、"
        "中高强度活动、有效站立、体重和血压。日期格式为 YYYY-MM-DD，days 为 1 到 30。"
    ),
    annotations=READ_ONLY,
)
async def get_health_history(
    metric: Metric,
    relative: str | None = None,
    query_date: str | None = None,
    days: int = 1,
) -> dict:
    return await health_bridge.history(relative, metric, query_date, days)


@mcp.tool(
    title="检查健康桥状态",
    description="检查小米凭证是否已经配置；不会返回任何密钥或完整账号信息。",
    annotations=READ_ONLY,
)
async def get_bridge_status() -> dict:
    return health_bridge.safe_status()


def _no_store(response: HTMLResponse | JSONResponse | RedirectResponse):
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


def _setup_authorized(request: Request) -> bool:
    cookie = request.cookies.get("boyuan_setup", "")
    return signer.verify(cookie, "setup_session") is not None


async def home(_: Request) -> HTMLResponse:
    body = """<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width'>
    <title>柏渊健康桥</title><style>body{font:16px system-ui;max-width:720px;margin:60px auto;padding:0 20px;line-height:1.7}a{color:#165dff}</style>
    <h1>柏渊健康桥</h1><p>小米运动健康亲友共享数据的只读 MCP 服务。</p>
    <p><a href='/setup'>配置小米登录</a> · <a href='/health'>服务状态</a></p>"""
    return HTMLResponse(body)


async def health(_: Request) -> JSONResponse:
    return JSONResponse(health_bridge.safe_status())


async def oauth_metadata(_: Request) -> JSONResponse:
    return JSONResponse(
        {
            "issuer": settings.public_base_url,
            "authorization_endpoint": f"{settings.public_base_url}/authorize",
            "token_endpoint": f"{settings.public_base_url}/token",
            "scopes_supported": ["health.read"],
            "response_types_supported": ["code"],
            "grant_types_supported": ["authorization_code", "refresh_token"],
            "code_challenge_methods_supported": ["S256"],
            "token_endpoint_auth_methods_supported": ["none"],
            "client_id_metadata_document_supported": True,
            "authorization_response_iss_parameter_supported": True,
        }
    )


async def approve_get(request: Request) -> HTMLResponse:
    ticket = request.query_params.get("ticket", "")
    if not signer.verify(ticket, "approval"):
        return _no_store(HTMLResponse("授权请求已失效，请返回 ChatGPT 重试。", status_code=400))
    safe_ticket = html.escape(ticket, quote=True)
    body = f"""<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width'>
    <title>授权柏渊健康桥</title><style>body{{font:16px system-ui;max-width:520px;margin:50px auto;padding:0 20px;line-height:1.7}}input,button{{box-sizing:border-box;width:100%;padding:12px;margin-top:12px;font-size:16px}}button{{background:#111;color:white;border:0;border-radius:8px}}</style>
    <h1>授权健康数据读取</h1><p>允许 ChatGPT 只读访问你已经共享给此小米账号的健康数据。</p>
    <form method=post action='/approve'><input type=hidden name=ticket value='{safe_ticket}'>
    <label>连接密码<input type=password name=password autocomplete=current-password required></label>
    <button type=submit>确认授权</button></form><p><small>不会授予修改、邀请或删除权限。</small></p>"""
    return _no_store(HTMLResponse(body))


async def approve_post(request: Request):
    form = await request.form()
    password = str(form.get("password", ""))
    ticket = str(form.get("ticket", ""))
    if not hmac.compare_digest(password.encode(), settings.owner_secret.encode()):
        return _no_store(HTMLResponse("连接密码不正确。", status_code=403))
    redirect_url = oauth_provider.approve(ticket)
    if not redirect_url:
        return _no_store(HTMLResponse("授权请求已失效，请返回 ChatGPT 重试。", status_code=400))
    return _no_store(RedirectResponse(redirect_url, status_code=302))


async def setup_get(request: Request):
    if _setup_authorized(request):
        return RedirectResponse("/setup/home", status_code=303)
    body = """<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width'>
    <title>配置柏渊健康桥</title><style>body{font:16px system-ui;max-width:520px;margin:50px auto;padding:0 20px;line-height:1.7}input,button{box-sizing:border-box;width:100%;padding:12px;margin-top:12px;font-size:16px}button{background:#111;color:white;border:0;border-radius:8px}</style>
    <h1>配置小米登录</h1><p>先输入部署时设置的连接密码。</p><form method=post action='/setup/login'>
    <label>连接密码<input type=password name=password autocomplete=current-password required></label><button type=submit>进入配置</button></form>"""
    return _no_store(HTMLResponse(body))


async def setup_login(request: Request):
    form = await request.form()
    password = str(form.get("password", ""))
    if not hmac.compare_digest(password.encode(), settings.owner_secret.encode()):
        return _no_store(HTMLResponse("连接密码不正确。", status_code=403))
    cookie = signer.issue("setup_session", {"sub": "owner"}, ttl_seconds=1800)
    response = RedirectResponse("/setup/home", status_code=303)
    response.set_cookie(
        "boyuan_setup",
        cookie,
        max_age=1800,
        httponly=True,
        secure=settings.public_base_url.startswith("https://"),
        samesite="strict",
    )
    return _no_store(response)


async def setup_home(request: Request):
    if not _setup_authorized(request):
        return RedirectResponse("/setup", status_code=303)
    configured = "已配置" if health_bridge.configured else "尚未配置"
    body = f"""<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width'>
    <title>小米登录</title><style>body{{font:16px system-ui;max-width:600px;margin:50px auto;padding:0 20px;line-height:1.7}}button{{width:100%;padding:13px;font-size:16px;background:#111;color:#fff;border:0;border-radius:8px}}</style>
    <h1>小米登录</h1><p>当前状态：{configured}</p><p>点击后会生成小米登录二维码，也可以在同一部手机上打开登录链接。</p>
    <form method=post action='/setup/start'><button type=submit>生成小米登录码</button></form>"""
    return _no_store(HTMLResponse(body))


async def _run_qr_login() -> None:
    auth = XiaomiAuth()

    async def on_qr(qr_image_url: str, login_url: str) -> None:
        setup_state.qr_image_url = qr_image_url
        setup_state.login_url = login_url
        setup_state.status = "waiting"

    try:
        token = await auth.login_qr(qr_callback=on_qr, max_wait=300)
        token_json = token.model_dump_json(indent=2)
        health_bridge.set_token_json(token_json)
        setup_state.token_json = token_json
        setup_state.status = "done"
    except Exception as exc:
        setup_state.error = str(exc)
        setup_state.status = "error"
    finally:
        await auth.close()


async def setup_start(request: Request):
    if not _setup_authorized(request):
        return RedirectResponse("/setup", status_code=303)
    if setup_state.task and not setup_state.task.done():
        setup_state.task.cancel()
    setup_state.status = "starting"
    setup_state.qr_image_url = ""
    setup_state.login_url = ""
    setup_state.error = ""
    setup_state.token_json = ""
    setup_state.task = asyncio.create_task(_run_qr_login())
    return RedirectResponse("/setup/status", status_code=303)


async def setup_status(request: Request):
    if not _setup_authorized(request):
        return RedirectResponse("/setup", status_code=303)
    if setup_state.status in {"starting", "waiting"}:
        qr = (
            f"<img src='{html.escape(setup_state.qr_image_url, quote=True)}' alt='小米登录二维码' style='max-width:100%'>"
            if setup_state.qr_image_url
            else "<p>正在获取登录码……</p>"
        )
        link = (
            f"<p><a href='{html.escape(setup_state.login_url, quote=True)}' target='_blank' rel='noreferrer'>在浏览器打开小米登录</a></p>"
            if setup_state.login_url
            else ""
        )
        content = f"<meta http-equiv=refresh content=3><h1>请登录小米账号</h1>{qr}{link}<p>页面会自动检查登录结果。</p>"
    elif setup_state.status == "done":
        token = html.escape(health_bridge.export_token_json())
        content = f"""<h1>登录成功</h1><p>先复制下面的 JSON，然后到 Render 的 Environment 新增 <code>MI_TOKEN_JSON</code> 并粘贴保存。</p>
        <textarea readonly style='width:100%;height:300px'>{token}</textarea><p>保存环境变量后，这一页中的临时凭证会随服务重启清除。</p>"""
    elif setup_state.status == "error":
        content = f"<h1>登录没有完成</h1><p>{html.escape(setup_state.error)}</p><p><a href='/setup/home'>重新生成</a></p>"
    else:
        content = "<h1>尚未开始</h1><p><a href='/setup/home'>返回配置</a></p>"
    body = f"""<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width'>
    <title>小米登录状态</title><style>body{{font:16px system-ui;max-width:620px;margin:40px auto;padding:0 20px;line-height:1.7}}textarea{{font:12px ui-monospace,monospace}}a{{color:#165dff}}</style>{content}"""
    return _no_store(HTMLResponse(body))


mcp_app = mcp.streamable_http_app(
    streamable_http_path="/mcp",
    stateless_http=True,
    json_response=True,
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
)


@contextlib.asynccontextmanager
async def lifespan(_: Starlette):
    async with mcp.session_manager.run():
        yield


app = Starlette(
    routes=[
        Route("/", home, methods=["GET"]),
        Route("/health", health, methods=["GET"]),
        Route("/.well-known/oauth-authorization-server", oauth_metadata, methods=["GET"]),
        Route("/approve", approve_get, methods=["GET"]),
        Route("/approve", approve_post, methods=["POST"]),
        Route("/setup", setup_get, methods=["GET"]),
        Route("/setup/login", setup_login, methods=["POST"]),
        Route("/setup/home", setup_home, methods=["GET"]),
        Route("/setup/start", setup_start, methods=["POST"]),
        Route("/setup/status", setup_status, methods=["GET"]),
        Mount("/", app=mcp_app),
    ],
    lifespan=lifespan,
)
