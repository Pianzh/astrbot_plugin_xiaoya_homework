<div align="center">

# astrbot_plugin_xiaoya_homework

**小雅作业提醒** · AstrBot 插件

扫码绑定小雅（理工智课）账号，定时抓未完成的课程任务推给你。

[![AstrBot](https://img.shields.io/badge/AstrBot-4.0%2B-orange.svg)](https://github.com/Soulter/AstrBot)
[![Python](https://img.shields.io/badge/Python-3.10%2B-blue.svg)](https://www.python.org/)
[![License](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)

</div>

## 💡 介绍

- **扫码登录**：发个指令，二维码直接推到 QQ，扫一下就绑好了，不用手动翻 cookie
- **只读**：只查询，不提交任何任务、不刷任何时长
- **凭证自动续期**：快过期时自己换新凭证，提醒不会因为 token 到期而断掉
- **到期提前提醒**：续期凭证快没的时候推一条，让你有从容扫码的余地
- **增量推送**：只推新出现的任务，不重复骚扰
- **临期催办**：快到截止时间时再催一次，每个任务只催一次
- **按课程分组**：清单按课程排好，组内按紧急度排序

## 📦 安装

在 AstrBot 面板的插件管理里填仓库地址：

```
https://github.com/PiannZH/astrbot_plugin_xiaoya_homework
```

或者命令行：

```bash
cd <astrbot>/data/plugins
git clone https://github.com/PiannZH/astrbot_plugin_xiaoya_homework.git
```

装完重启 AstrBot。

## ⌨️ 使用

| 指令 | 说明 |
|:---:|:---|
| `/小雅登录` | 生成二维码，扫码绑定。凭证过期了直接再发一次，会覆盖旧的 |
| `/小雅绑定 <token>` | 手动换凭证（扫码失败时的兜底） |
| `/小雅解绑` | 清空凭证和推送记录 |
| `/小雅作业 [天数]` | 手动查未完成作业，不带参数查全部 |
| `/小雅推送 [开\|关]` | 定时推送开关，不带参数看当前状态和推送目标 |
| `/小雅状态` | 看凭证有效性、推送开关和轮询情况 |
| `/小雅帮助` | 指令列表 |

扫码步骤：发 `/小雅登录` → 收到二维码 → 打开「小雅」App，首页右上角「扫一扫」→ 手机上确认 → 机器人回你绑定成功。

### 推送到哪

默认推到你执行 `/小雅登录` 或 `/小雅绑定` 的那个会话，存在插件自己的 `state.json` 里。发 `/小雅推送` 随时可以看当前目标。

要换地方就改配置里的 `push_sessions`，填 `unified_msg_origin`，比如 `aiocqhttp:群:123456`。

## ⚙️ 配置

| 配置项 | 默认 | 说明 |
|---|---|---|
| `school` | `whut` | 学校标识。`whut` = 武汉理工大学（理工智课），`ccnu` = 华中师范大学 |
| `access_token` | 空 | 一般不用手填，扫码自动写入 |
| `refresh_token` | 空 | 扫码自动写入，用于自动续期 |
| `auto_refresh` | `true` | 快过期时自动换新凭证 |
| `refresh_margin_hours` | `12` | 访问凭证剩不到这个小时数就提前续 |
| `expiry_warn_hours` | `48` | 续期凭证剩不到这个小时数就推提醒，设 0 关闭 |
| `check_interval_minutes` | `30` | 轮询间隔（分钟），别低于 5 |
| `remind_hours` | `24` | 临期催办阈值（小时），设 0 关闭 |
| `notify_new` | `true` | 是否推送新任务 |
| `notify_urgent` | `true` | 是否推送临期催办 |
| `push_enabled` | `true` | 定时推送总开关，关掉后指令查询照常 |
| `push_sessions` | 空 | 推送目标，留空则推到绑定时的会话 |
| `proxy` | 空 | HTTP 代理，小雅服务器在国内，一般不用 |
| `qr_ttl_seconds` | `60` | 二维码有效期，30~180 |
| `qr_debug` | `false` | 登录失败时把诊断信息推到 QQ，排查时打开，定位完关掉 |

## 🔐 凭证是怎么来的

小雅没有开放密码登录接口，网页登录后 cookie 里有个 `prd-access-token`，请求时以 `Authorization: Bearer <token>` 发送。

这个插件复刻了官方登录页（`infra.ai-augmented.com/app/auth`）的扫码流程：

1. `GET /api/auth/qrLogin/getCode` 拿二维码地址
2. `GET /api/auth/qrLogin/getCodeStatus` 每秒轮询，等你在手机上确认。状态翻到「已确认」时服务端会把 infra 会话 cookie 下发给正在轮询的客户端
3. `GET /api/auth/login/listAccounts` 列出账号，`POST /api/auth/login/bySelectAccount` 选定——这一步才真正建立会话
4. `GET /api/auth/oauth/onAccountAuthRedirect`（不带参数，服务端凭会话 302）拿到回调地址
5. 访问该地址，从 `Set-Cookie` 里取 `WT-prd-access-token`

其中第 5 步的地址要**原样请求**，不能自己拿 code 拼——`schoolCode` 是必填参数，漏了学校站点会回 500。

登录时学校站点还会下发一个 `WT-prd-refresh-token`。不存它的话，访问凭证十来小时就过期、续期凭证只给 24 小时，等于**每天都得重扫**。存下来之后插件会用它自动续期：

```
POST /api/jx-auth/oauth2/token
Authorization: Basic <orgToken>          # 从 /api/jw-starcmooc/base/school/gainReactApp 取
Content-Type: application/x-www-form-urlencoded

grant_type=refresh_token
refresh_token=<WT-prd-refresh-token>
token_time=604800000                      # 7 天，官方「记住我」的值
```

返回里直接带两个凭证的过期时刻，所以「还剩多久」不用估算。`token_time` 服务端不校验上限，但 7 天是官方前端唯一会发的值，不多要。

token 只写进 AstrBot 自己的配置文件，不进日志、不外传。

### 手动绑定（兜底）

浏览器登录小雅 → F12 → Application → Cookies → 复制 `WT-prd-access-token` 的值，然后：

```
/小雅绑定 <粘过来的值>
```

## 🔧 支持的学校

小雅是按学校分配子域名的多租户平台，`core/client.py` 里的 `SCHOOLS` 决定用哪个站点。

| key | 学校 | 站点 |
|:---:|:---|:---|
| `whut` | 武汉理工大学（理工智课） | `whut.ai-augmented.com` |
| `ccnu` | 华中师范大学（小雅） | `ccnu.ai-augmented.com` |

**加新学校**：往 `SCHOOLS` 里加一条，`client_id` 填 `xy_client_<学校缩写>`，`redirect_uri` 填 `<站点域名>/api/jw-starcmooc/user/authorCallback`，`school_code` 从小雅接口 `/api/auth/login/listSchoolsByClient` 里查。学校清单也可以打 `POST https://catalog.ai-augmented.com/api/application/searchApplicationConfig`（`Authorization: Basic eHlhdXRoX3NlcnZlcjoxeDhkTXNaMDJ6ZzBjN3BLMk9YN0NZVlM=`，body `{"appName":"prd_app_portal"}`）拿。

## 🧪 开发

```bash
pip install -r requirements.txt
pip install pytest ruff

pytest tests/ -q          # 单元测试，37 个
ruff check .              # 静态检查

# 集成冒烟测试（会打真实接口）
SMOKE_PROXY=http://127.0.0.1:7897 python tests/smoke_live.py
```

## ❗ 免责声明

本插件只做查询和提醒，不会替你提交任务或刷时长。请自己按时完成作业，遵守平台规则。因使用本插件导致的后果（漏交、账号异常等）由你自己承担。

## 📄 许可证

[MIT](LICENSE)

协议实现思路参考了社区项目 [XiaoYaEasyTasks](https://github.com/zygame1314/XiaoYaEasyTasks)（MIT），任务类型映射和紧急度权重沿用其定义，特此致谢。
