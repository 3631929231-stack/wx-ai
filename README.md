# 微信里的 DeepSeek · 部署指南

## 为什么是这个方案

你想「在微信里跟 DeepSeek 像真人一样聊，而且不开电脑」。

能同时满足「微信里」+「不开电脑」的，只有一条路：**微信公众号**。

| 方案 | 在微信里 | 不用电脑 | 可行性 |
|---|---|---|---|
| 电脑上 hook 微信（wxauto / WeChatFerry） | ✅ | ❌ 得开着电脑 | 你电脑是微信 4.1.15，工具链还不稳 |
| 微信个人号协议（PadLocal 之类） | ✅ | ✅ | 号会被封，且要买授权 |
| **微信公众号** | ✅ | ✅ | 个人免费注册，用微信本身聊天 |

代价说清楚：

- **个人只能注册订阅号**，订阅号只能在用户发消息后 5 秒内**被动回复**，不能主动推给你
- 想要主动推送（比如每天早上提醒你）要用服务号，服务号要企业营业执照

所以最终形态是**两件套**：

```
你问它答 ──▶ 公众号（这个服务，24 小时在线，不用电脑）
定时提醒 ──▶ Bark / Server酱（agent/ 那套，推到你手机通知栏或微信）
```

其实这就够了：真人朋友也不会每天主动刷你，你问它才答。

---

## 一、注册订阅号（10 分钟，免费）

1. 打开 <https://mp.weixin.qq.com>，点「立即注册」→ **订阅号**
2. 用一个**没注册过公众号的邮箱**，密码自己设
3. 主体类型选**个人**，填身份证 + 微信扫码验证
4. 名称随便起（比如「我的学习搭子」），介绍随便写
5. 注册完进后台

个人订阅号不需要审核费、不需要营业执照。

---

## 二、把服务部署上线

服务是**零依赖纯标准库**写的，只要有 Python 3.9+ 就能跑。

### 方案 A：Render（免费，最省事）

1. 把 `wechat-todo` 这个目录推到你的 GitHub 仓库
2. <https://render.com> 注册，New → **Web Service** → 连你的仓库
3. 填：
   - Root Directory: `wx-ai`
   - Runtime: `Python 3`
   - Build Command: `pip install -r requirements.txt`
   - Start Command: `gunicorn -w 1 --threads 4 -b 0.0.0.0:$PORT app:app`
4. Environment 里加三个变量：

   | Key | Value |
   |---|---|
   | `WX_TOKEN` | 自己编一串，比如 `ygwx2026abc` |
   | `DEEPSEEK_API_KEY` | `sk-...` |
   | `SELF_NICKNAME` | 你的名字（可选） |

5. 部署完拿到地址，形如 `https://xxx.onrender.com`

**务必做的一步**：Render 免费实例 15 分钟没请求会休眠，冷启动 30 秒——微信只等 5 秒，会直接失败。

去 <https://uptimerobot.com> 注册，加一个 HTTP 监控，地址填 `https://xxx.onrender.com/health`，间隔 5 分钟。这样它就不会睡。

### 方案 B：Railway / Zeabur（几块钱，不休眠）

流程差不多，`Procfile` 已经写好了，直接识别。免费额度用完大概 5 美元/月，比 Render 省心。

### 方案 C：自己的 VPS

```bash
cd wx-ai
pip install -r requirements.txt
export WX_TOKEN=你的token
export DEEPSEEK_API_KEY=sk-...
gunicorn -w 1 --threads 4 -b 0.0.0.0:8000 app:app
```

前面挂个 Nginx + 证书，或者直接用 `https://你的域名/wx`。

### 方案 D：先本地验证（推荐先做这一步）

```bash
export WX_TOKEN=testtoken123
export DEEPSEEK_API_KEY=sk-你的
python app.py
# 另一个终端
python test_wx.py
```

看到 `通过 15，失败 0` 就说明协议层没问题。

---

## 三、接上公众号

公众号后台 → 左侧 **设置与开发** → **基本配置** → **服务器配置** → 修改配置：

| 字段 | 填什么 |
|---|---|
| URL | `https://xxx.onrender.com/wx` ← 注意结尾的 `/wx` |
| Token | 和环境变量 `WX_TOKEN` **完全一致** |
| EncodingAESKey | 点「随机生成」 |
| 消息加解密方式 | **明文模式** |

点「提交」，微信会立刻给你的服务发一个 GET 请求验证。成功会提示「提交成功」。

然后点 **启用**。

> 如果提示「token验证失败」，99% 是 `WX_TOKEN` 没对上，或者服务在休眠。先访问一下 `https://xxx.onrender.com/health` 把它叫醒，再点提交。

---

## 四、用起来

微信里搜你公众号的名字，关注，然后就能聊了。

**普通聊天**

> 你：今天好累啊
> 它：累就歇会儿，别硬撑。明天有啥要交的吗？

**把群消息转给它**

> 你：（转发学委的通知）周五晚上8点之前把实验报告发给我，PDF格式
> 它：记下了，周五晚上 8 点前交实验报告，PDF。（记下了：周五晚上8点之前把实验报告发给我，2026-10-30 20:00 前）

**查任务**

> 你：待办
> 它：记着这些：周五晚上8点之前把实验报告（2026-10-30 20:00）、下周三组会（2026-11-04 14:00）。别的没了。

它记得住上下文，可以连续追问：「那个报告要几页」「帮我起个提纲」。

---

## 五、它是怎么"像真人"的

人设写在 `app.py` 的 `PERSONA` 里，核心几条：

- 一次只回一两句，最多三句
- 不用 markdown、不用列表、不用编号
- 不说"好的""收到""很高兴为你服务"
- 可以用"嗯""行""那""欸"起头，可以反问
- 拿不准就说拿不准

参数上：`temperature=1.3`（比默认活泼）、`max_tokens=220`（强制短，也保证 5 秒内能回）。

想调性格直接改 `PERSONA`，改完重启即可。

---

## 六、已知限制

| 限制 | 说明 | 绕法 |
|---|---|---|
| 5 秒必须回复 | 超了微信会重试 3 次，用户看到「该公众号暂时无法提供服务」 | 已把 `max_tokens` 压到 220，实测够；重试命中 MsgId 缓存，不会重复消耗 |
| 不能主动推 | 个人订阅号没有客服消息接口 | 定时提醒走 `agent/` → Bark / Server酱 |
| 上下文存在内存 | 服务重启就清空 | 单实例够用；要持久化就把 `HISTORY` 换成 Redis |
| 待办存在 `/tmp` | Render 重启会丢 | 挂一个 Persistent Disk 到 `/data`，然后设 `DATA_DIR=/data` |

---

## 七、接口

| 方法 | 路径 | 作用 |
|---|---|---|
| GET/POST | `/wx` | 微信服务器回调（签名校验 + 收发消息） |
| GET | `/health` | 健康检查，保活用 |
| POST | `/admin/push?secret=xxx` | 外部程序塞待办进来 `{"openid":"...","tasks":[...]}` |

`/admin/push` 可以让你在电脑上跑完 `agent/digest.py` 之后，把结果同步进公众号的待办本。openid 从日志里能看到，或者先在公众号里发条消息，然后看 `wx_todos.json`。

---

## 八、文件

| 文件 | 作用 |
|---|---|
| `app.py` | 全部逻辑：签名校验、XML 收发、人设、待办本、DeepSeek 调用 |
| `test_wx.py` | 15 个用例，模拟微信服务器，不需要联网 |
| `requirements.txt` | 只有 gunicorn |
| `Procfile` | Render / Railway 识别用 |
