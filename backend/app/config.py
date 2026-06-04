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
