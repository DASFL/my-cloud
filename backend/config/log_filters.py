import logging
import re


class RedactShareTokens(logging.Filter):
    share_path = re.compile(r"(/api/shared/)[^/\s?#\"']+")

    def filter(self, record):
        message = record.getMessage()
        redacted = self.share_path.sub(r"\1[redacted]", message)
        if redacted != message:
            record.msg = redacted
            record.args = ()
        return True
