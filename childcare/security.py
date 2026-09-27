"""家庭标识匿名化: 服务端只保留 HMAC 伪名, 原始标识即用即弃。"""

import hashlib
import hmac


def pseudonym(secret: bytes, family_ref: str) -> str:
    """把家庭标识(如证件号)映射为不可逆伪名, 用于候补去重。

    原始标识不参与存储与日志; 没有服务端密钥无法反推,
    任何角色(含审计)都只能看到聚合计数。
    """
    if not isinstance(family_ref, str) or not family_ref.strip():
        from .errors import ValidationError
        raise ValidationError("家庭标识不能为空")
    return hmac.new(secret, family_ref.strip().encode("utf-8"),
                    hashlib.sha256).hexdigest()
