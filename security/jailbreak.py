import re
import math
import unicodedata
from collections import Counter


class JailbreakDetector:
    THRESHOLD = 15
    _URL_CODE_CHARS = set('://.@#&=?%-_+~')

    PATTERNS: list[tuple[str, int]] = [
        (r'ignore\s*(previous|above|all|prior|earlier|instructions?)', 15),
        (r'disregard\s*(previous|above|all|prior)', 15),
        (r'forget\s*(everything|previous|instructions?|all|prior)', 15),
        (r'system\s*(prompt|instructions?|message|directive)', 12),
        (r'developer\s*mode|admin\s*mode|god\s*mode', 15),
        (r'jailbreak|jail\s*break', 15),
        (r'unut\s*(her\s*sey|talimat|onceki)', 15),
        (r'gormezden\s*gel', 12),
        (r'(sifre|parola|flag|bayrak)\w*\s*(ver|goster|soyle)', 15),
        (r'(sistem|system)\s*(komutu?|talimat|mesaj|yonerg)', 12),
        (r'gelistirici\s*modu', 15),
        (r'yonetici\s*modu', 15),
        (r'<\s*system\s*>|<\s*admin\s*>|<\s*root\s*>', 15),
        (r'\[system\]|\[admin\]|\[root\]', 15),
        (r'```system|```admin', 15),
        (r'###\s*new\s*instruction', 15),
        (r'---\s*ignore\s*above', 15),
        (r'%%\s*override', 15),
        (r'i\'?m\s*(your|the)\s*(creator|developer|admin|owner)', 12),
        (r'(ben|i\s*am)\s*(gelistiric|developer|admin|yonetici)', 12),
        (r'what\s*(are|is)\s*your\s*(instruction|prompt|rule)', 12),
        (r'(talimat|kural|yonerg).*nedir', 12),
        (r'how\s*were\s*you\s*programmed', 12),
        (r'nasil\s*programland', 12),
        (r'(return|print|display|show|echo|goster)\s*(the|your)?\s*(prompt|instruction|system|flag|secret)', 12),
        (r'you\s*are\s*(now|actually|really|truly)', 8),
        (r'act\s*as\s*(if|a|an|though)', 6),
        (r'pretend\s+(you|to\s+be|that\s+you)', 8),
        (r'roleplay\s*(as|that|mode)', 8),
        (r'role\s*play\s*(as|that|mode)', 8),
        (r'simulate\s+(being|a|an|that)', 8),
        (r'bypass\s*(security|filter|restriction)', 10),
        (r'override\s*(system|instruction|security|rule)', 10),
        (r'rol\s*yap|rol\s*al', 8),
        (r'new\s*(session|conversation|context|chat)', 6),
        (r'reset\s*(yourself|context|memory|conversation)', 6),
        (r'start\s*(over|fresh|new)', 5),
        (r'output\s*as\s*(json|xml|code|list|array)', 6),
        (r'format.*as\s*(json|xml|code|raw)', 6),
        (r'authorized|yetkili', 6),
        (r'you\s*(can|should|must)\s*not', 5),
        (r'it\'?s\s*(ok|okay|fine)\s*to\s*(reveal|show|tell|share)', 8),
        (r'don\'?t\s*worry\s*(about|it)', 5),
        (r'base64|rot13|rot\s*13|caesar', 4),
        (r'(decode|encode|coz|kodla)\s*(this|the|flag|secret)', 6),
        (r'hex|hexadecimal', 3),
        (r'(translate|cevir).*to.*(english|spanish|french)', 4),
        (r'imagine\s+(you|that|a\s+world|a\s+scenario)', 5),
        (r'suppose\s+(you|that|we)', 4),
        (r'what\s*if\s+(you|your|the\s+system|i\s+told)', 5),
        (r'hypothetically\s*(speaking|,)?\s*(if|you|what)', 5),
        (r'in\s*a\s*(world|universe|scenario|reality)\s*where', 5),
        (r'let\'?s\s*say\s+(you|your|the)', 4),
        (r'diyelim\s*ki', 4),
        (r'please\s+please|lutfen\s+lutfen', 3),
        (r'i\s*(need|require|must\s*have)\s*(the|your|a)\s*(flag|secret|password|key)', 8),
        (r'(help|yardim)\s*me.*urgent', 3),
        (r'en\s*español|auf\s*deutsch|en\s*français', 4),
        (r'turkce.*ingilizce|english.*turkish', 3),
        (r'restart|reboot|initialize', 4),
        (r'bicimlendir', 3),
        (r'binary|ascii|unicode', 2),
    ]

    def detect(self, text: str) -> tuple[bool, str | None]:
        normalized = unicodedata.normalize('NFKD', text.lower())
        normalized = ''.join(c for c in normalized if not unicodedata.combining(c))
        # Fix Turkish dotless i (ı) which NFKD doesn't decompose
        normalized = normalized.replace('ı', 'i')
        compressed = re.sub(r'[\s\-_\.,:;!?]+', '', normalized)

        total_score = 0
        matched = []

        for pattern, weight in self.PATTERNS:
            if re.search(pattern, normalized, re.IGNORECASE) or re.search(pattern, compressed, re.IGNORECASE):
                total_score += weight
                matched.append(f"{pattern[:30]}...(+{weight})")
                if total_score >= self.THRESHOLD * 2:
                    break

        if len(text) > 50 and self._calculate_entropy(text) > 5.5:
            total_score += 4
            matched.append("Entropy(+4)")
        if self._check_repetition(text):
            total_score += 10
            matched.append("Repetition(+10)")
        if self.check_delimiters(text):
            total_score += 8
            matched.append("Delimiters(+8)")

        special = sum(1 for c in text if not c.isalnum() and not c.isspace() and c not in self._URL_CODE_CHARS)
        if len(text) > 20 and special / len(text) > 0.35:
            total_score += 6
            matched.append("Special(+6)")
        if text.count('?') > 8:
            total_score += 4
            matched.append("Questions(+4)")

        if total_score >= self.THRESHOLD:
            return True, f"Score {total_score}: " + ", ".join(matched[:3])
        return False, None

    @staticmethod
    def _calculate_entropy(text: str) -> float:
        if not text or len(text) < 10:
            return 0.0
        counts = Counter(text)
        probs = [c / len(text) for c in counts.values()]
        return -sum(p * math.log2(p) for p in probs if p > 0)

    @staticmethod
    def _check_repetition(text: str) -> bool:
        words = text.lower().split()
        if len(words) < 5:
            return False
        return max(Counter(words).values()) > len(words) * 0.4

    @staticmethod
    def check_delimiters(text: str) -> bool:
        patterns = [r'<[^>]*>', r'\[.*\]', r'```', r'---+', r'===+', r'\*\*\*', r'#{2,}']
        return sum(len(re.findall(p, text)) for p in patterns) > 3

