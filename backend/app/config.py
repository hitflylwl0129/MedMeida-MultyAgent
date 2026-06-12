"""集中配置：所有密钥/参数从环境变量(.env) 读取，禁止硬编码。"""
from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # 腾讯云密钥
    tencentcloud_secret_id: str = ""
    tencentcloud_secret_key: str = ""

    # 云点播
    vod_sub_app_id: int = 0
    vod_region: str = "ap-guangzhou"

    # AIGC 生视频默认参数
    aigc_model_name: str = "Kling"
    aigc_model_version: str = "2.1"
    aigc_scene_type: str = "avatar_i2v"
    aigc_aspect_ratio: str = "9:16"
    aigc_audio_generation: str = "Enabled"
    aigc_input_compliance: str = "Enabled"
    aigc_output_compliance: str = "Enabled"
    aigc_person_generation: str = "AllowAdult"
    aigc_storage_mode: str = "Temporary"

    # 医生形象图
    doctor_image_fileid: str = ""
    doctor_image_url: str = ""
    # 默认出镜医生形象（形象库 key：senior/middle/young _ male/female）
    default_doctor: str = "middle_male"

    # ---- 视频后端开关 ----
    # local：本地 TTS+ffmpeg 拼"医生静帧+真实口播"（默认；无需白名单）
    # motion：腾讯云 motion_control（医生图+参考动作视频→医生动作迁移）+ 本地 TTS 拼音轨
    # aigc：调腾讯云 CreateAigcVideoTask（avatar_i2v，需要白名单）
    # kling：可灵原厂 API（image2video 造克制基础视频 → identify-face → advanced-lip-sync），
    #        口型与口播严格同步、嘴部幅度自然，最终烧字幕。需 KLING_ACCESS_KEY/SECRET_KEY。
    video_backend: str = "local"

    # ---- 可灵 Kling 原厂 API（路 A：image2video + advanced-lip-sync） ----
    kling_access_key: str = ""
    kling_secret_key: str = ""
    kling_base_url: str = "https://api-beijing.klingai.com"
    # image2video 造"动作克制的医生基础视频"
    kling_image_model: str = "kling-v1-6"
    kling_base_duration: str = "10"          # 单段基础视频时长（接口支持 5/10）
    kling_base_mode: str = "std"             # std / pro
    kling_base_cfg_scale: float = 0.5
    kling_base_prompt: str = (
        "医生端庄站立，面对镜头，表情专业亲和，嘴唇自然闭合保持微笑，"
        "仅有轻微的眨眼和极小幅度的点头，身体基本静止，不说话，画面稳定，无夸张表情"
    )
    kling_base_negative_prompt: str = "张嘴说话，夸张表情，剧烈晃动，大幅动作，变形"
    # 轮询
    kling_poll_interval_sec: int = 10
    kling_poll_timeout_sec: int = 600
    # 给可灵拉取素材（基础视频）用的公网基地址；留空则回退 public_base_url。
    # 注意：identify-face 需要从公网拉取我们暴露的基础视频，必须是可灵可达的绝对地址。
    kling_public_base_url: str = ""

    # ---- 腾讯云数智人「照片免训练」（v1.3 引擎二）---- #
    # 文档：https://cloud.tencent.com/document/product/1240/118475
    # 鉴权：HMAC-SHA256(AccessToken, sorted_query) → Base64 → URLEncode（全部在 URL Query）
    # 详见调研报告：腾讯云数智人照片免训练_调研.md
    tencent_avatar_app_key: str = ""
    tencent_avatar_access_token: str = ""
    # 文本驱动的默认音色（不同医生在 doctors.tts_voice_for_doctor_avatar 里做映射）
    tencent_avatar_default_timbre: str = "male_1"
    # 接口 TTS 实测比 v1.1 Kling 慢 ~31%（17s 文本读 22s），用 1.2 抵消
    tencent_avatar_speech_speed: float = 1.2
    # 分辨率档位："720P" / "1080P"（内部转 int 0/1）
    tencent_avatar_resolution: str = "720P"
    # 提交 / 轮询参数
    tencent_avatar_poll_interval_sec: int = 8
    tencent_avatar_poll_timeout_sec: int = 900   # 17s 话术约 5-6 分钟，15 分钟兜底
    # 试用账号并发=1，遇到 100008 时重试（任务可能已入队）
    tencent_avatar_submit_retries: int = 6
    tencent_avatar_submit_retry_interval_sec: int = 30
    # 输出比例 5:7.4 → 后处理到 9:16（cover 中心裁切 / pad 上下补黑）
    tencent_avatar_target_aspect: str = "9:16"
    tencent_avatar_fit_mode: str = "cover"   # cover=中心裁切（推荐）| pad=补黑边

    @property
    def tencent_avatar_ready(self) -> bool:
        return bool(self.tencent_avatar_app_key and self.tencent_avatar_access_token)

    # ---- motion_control 默认参数 ----
    motion_ref_filename: str = "ref.mp4"   # 参考动作视频文件名（位于 backend/assets/motion_ref/）
    motion_model_name: str = "Kling"
    motion_model_version: str = "2.1"
    motion_aspect_ratio: str = "9:16"
    motion_poll_interval_sec: int = 6
    motion_poll_timeout_sec: int = 600

    # ---- TTS（TextToVoice）默认值 ----
    tts_region: str = "ap-guangzhou"
    # 默认走"超自然大模型音色"段（账号已持有该资源包；精品段 101xxx 会 PkgExhausted）
    # 详细映射见 doctors.tts_voice_for_doctor()
    tts_voice_type: int = 602003     # 爱小悠·中性女声，作为兜底
    tts_speed: float = 0.0           # 0=1.0x；范围 [-2,6]
    tts_volume: float = 0.0
    tts_sample_rate: int = 24000     # 大模型音色支持 24k，更清晰
    tts_emotion: str = ""            # 大模型音色不支持情感，留空

    # ---- 本地链路缓存（路线 Y） ----
    # backend/.cache/jobs/ 下保留最近 N 个任务目录，多余按 mtime 删除（0=不清理）
    local_keep_jobs: int = 20

    # ---- LLM（话术 Agent，OpenAI 兼容端点） ----
    llm_base_url: str = "https://api.lkeap.cloud.tencent.com/plan/v3"
    llm_api_key: str = ""
    llm_model: str = "hy3-preview"
    llm_temperature: float = 0.7
    llm_max_tokens: int = 600
    llm_timeout_sec: int = 60

    # ---- B站投稿（短视频分发 Agent · Web 投稿，Cookie 鉴权） ----
    # 凭证只在后端 .env，绝不下发前端 / 不入版本库。
    # 获取方式：浏览器登录 bilibili.com 后，开发者工具 → Application → Cookies 复制 SESSDATA 与 bili_jct。
    bili_sessdata: str = ""
    bili_jct: str = ""                       # CSRF token
    bili_buvid3: str = ""                    # 可选，部分风控场景需要
    bili_default_tid: int = 201              # 默认分区：知识区 → 科学科普(201)
    bili_default_tag: str = "健康科普,科普,养生"
    bili_default_copyright: int = 1          # 1=自制 / 2=转载
    bili_only_self: int = 1                  # 1=仅自己可见(安全默认) / 0=公开
    bili_upload_profile: str = "ugcfx/bup"
    bili_timeout_sec: int = 90

    # ---- 腾讯云 Agent Runtime（选品 Agent 沙箱，E2B 协议）----
    # 文档：https://cloud.tencent.com/document/product/1814
    # 设计：编排留在本进程，仅"执行 LLM 生成代码 / 抓行情"借沙箱跑完即销毁。
    agr_enabled: bool = False
    e2b_domain: str = "ap-guangzhou.tencentags.com"
    e2b_api_key: str = ""
    agr_template_code: str = "code-medmedia-v1"
    agr_template_browser: str = "browser-medmedia-v1"
    agr_default_timeout_sec: int = 3600
    agr_code_run_timeout_sec: int = 600

    @property
    def agr_ready(self) -> bool:
        """选品 Agent 是否具备调用 AGR 的最小条件。"""
        return bool(self.agr_enabled and self.e2b_api_key)

    # ---- 选品 Agent v2.1（LLM 动态写解析代码） ----
    # 第 ② 步「数据解析」执行模式：
    #   "llm"       —— v2.1：LLM 看 schema 摘要后动态写 pandas（默认）
    #   "hardcoded" —— v2.0：写死的列名词典 + groupby（紧急回滚兜底）
    # 详见：选品Agent_LLM动态解析_v2.1设计.md
    product_parse_mode: str = "llm"
    # v2.1 LLM 解析重试上限（含首轮）。3 轮覆盖率/耗时性价比最优。
    product_parse_llm_max_rounds: int = 3
    # v2.1 LLM 出码生成时的 max_tokens / temperature
    # 代码生成温度低一点更稳定，max_tokens 给足以容纳 80 行 pandas
    product_parse_llm_temperature: float = 0.2
    product_parse_llm_max_tokens: int = 1500

    # ---- 访问统计（v1.0 access stats）---- #
    # 详见：访问统计_技术路线与原型.md
    # 本模块所有数据本地化，不出域。
    access_stats_enabled: bool = True
    # access.db 路径（独立 SQLite，与 jobs.db 隔离），相对 backend/
    access_db_path: str = ".cache/access.db"
    # 后台 BasicAuth 凭证（生产 .env 必须覆盖默认值）
    access_admin_user: str = "admin"
    access_admin_pass: str = "Demo2026"
    # 数据保留期（天），过期由定时任务归档 CSV 后删源表（PR-4 实现，PR-2 留 hook）
    access_retention_days: int = 90
    # IP 脱敏：admin 页展示是否对 IP 末段打码（192.168.1.*** ）
    # 用户拍板 Q7：一直开（不区分操作员级别）
    access_ip_mask_default: bool = True
    # ingest 限流：单 IP 每秒最多 N 次 /api/track/p.gif
    access_ingest_rate_per_sec: int = 5
    # GeoIP 数据库路径（MaxMind GeoLite2-City.mmdb），相对 backend/
    # 留空或文件不存在时跳过城市解析（不影响主链路）
    access_geoip_path: str = "data/GeoLite2-City.mmdb"
    # 心跳心跳判活窗口（秒）：>该窗口未上报心跳视为离开
    access_online_window_sec: int = 300
    # /api/track/footer（页脚条）数据缓存秒数（避免每次刷页都打 SQLite）
    access_footer_cache_sec: int = 5

    # 轮询
    poll_interval_sec: int = 10
    poll_timeout_sec: int = 900

    # 服务
    app_host: str = "127.0.0.1"
    app_port: int = 8000
    # 对外可访问的基地址（用于拼 output.url / cover_url 给前端播放）：
    #   - 留空（默认）：写相对路径 /api/...，浏览器按当前页 origin 自动拼，
    #     适配「同源 nginx 反代上线 + 本地 http://localhost:8848 反代」常见场景。
    #   - 显式填如 http://162.14.76.209 ：写绝对路径，兼容 file:// 直开 HTML 跨源场景。
    public_base_url: str = ""

    @property
    def credentials_ready(self) -> bool:
        return bool(
            self.tencentcloud_secret_id
            and self.tencentcloud_secret_key
            and self.vod_sub_app_id
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()
