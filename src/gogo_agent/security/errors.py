"""凭证相关的稳定错误：错误码给调用方判断，文本固定且不包含密钥、密文或 Token。"""


class SecurityError(Exception):
    """带稳定错误码的凭证错误基类；``message`` 只能是固定中文说明，不得拼接敏感值。"""

    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
