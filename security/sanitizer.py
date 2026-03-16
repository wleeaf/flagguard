import re
import logging
import hashlib
import random
import base64

import config
from ai.difficulty import get_difficulty_profile
from ai.personalities import MILD_SARCASM


class OutputSanitizer:
    def __init__(self, flag: str):
        self._flag = flag
        self._flag_hash = hashlib.sha256(flag.encode()).hexdigest()
        self._flag_parts = self._extract_parts(flag)

    def sanitize(self, text: str, user_input: str, *, profile=None) -> str:
        if profile is None:
            profile = get_difficulty_profile(None)

        if not profile.strict_exact_flag_block and not profile.strict_flag_format_block and profile.partial_flag_match_min_parts <= 0 and profile.leaked_keyword_block_threshold >= 999:
            return text

        text_lower = text.lower()

        if profile.strict_exact_flag_block:
            if self._flag and (self._flag in text or self._flag.lower() in text_lower):
                logging.critical("FLAG LEAK BLOCKED - Input: %s", user_input[:100])
                return "🛡️ Güvenlik sistemi devreye girdi. Nice try! 😏"

        if profile.strict_flag_format_block:
            if re.search(rf'{re.escape(config.FLAG_PREFIX)}\{{[^}}]{{3,}}\}}', text, re.IGNORECASE):
                logging.critical("🚨 FLAG FORMAT IN OUTPUT")
                return "🛡️ Neredeyse kandırıyordun beni! Ama olmadı. 😏"

        if self._flag_parts and profile.partial_flag_match_min_parts > 0:
            matched = [p for p in self._flag_parts if len(p) > 3 and p.lower() in text_lower]
            if len(matched) >= profile.partial_flag_match_min_parts:
                logging.warning("Partial flag match filtered (min=%d): %s", profile.partial_flag_match_min_parts, matched)
                return "🛡️ Şüpheli içerik filtrelendi. 😏"

        leaked_kw = [
            'directive', 'instruction', 'system prompt', 'core directive',
            'layer', 'talimat', 'sistem komutu', 'kural', 'restriction',
            'yönerge', 'defense', 'protection', 'filter',
        ]
        leaked_kw_hits = sum(1 for kw in leaked_kw if kw in text_lower)
        if leaked_kw_hits >= profile.leaked_keyword_block_threshold:
            return random.choice(MILD_SARCASM)

        # Require at least 10 base64 groups (40 chars), must contain + or / to
        # distinguish from normal alphanumeric text, and must end with valid padding.
        b64_match = re.search(r'(?:[A-Za-z0-9+/]{4}){10,}={0,2}', text)
        if b64_match:
            candidate = b64_match.group()
            has_b64_chars = any(c in '+/' for c in candidate)
            if has_b64_chars and not all(c in '0123456789abcdefABCDEF' for c in candidate):
                try:
                    decoded = base64.b64decode(candidate, validate=True)
                    if decoded and sum(1 for b in decoded if 32 <= b <= 126) / len(decoded) > 0.7:
                        return "🛡️ Kodlanmış içerik tespit edildi. Çok şüphelisin! 🕵️"
                except Exception:
                    pass

        if len(text) > 600:
            return "💬 Çok uzun konuşuyorum galiba. Kısacası: hayır. 🙃"

        return text

    def check_flag(self, user_input: str) -> str | None:
        if self._flag and self._flag in user_input.strip():
            logging.info("FLAG SOLVED! - Input: %s", user_input)
            return self.flag_success_message()
        return None

    def flag_success_message(self) -> str:
        """Return the flag-solved congratulations message."""
        lines = ["Tebrikler, doğru flag'i buldun."]
        if config.FLAG_SUCCESS_URL:
            lines.append("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
            lines.append(config.FLAG_SUCCESS_URL)
            lines.append("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
            lines.append("Sıradaki adım orada.")
        lines.append(f"- {config.BOT_NAME} INTERNAL")
        return "\n".join(lines) + "\n"

    @staticmethod
    def _extract_parts(flag: str) -> list[str]:
        if '{' in flag and '}' in flag:
            return flag.split('{')[1].split('}')[0].split('_')
        return []
