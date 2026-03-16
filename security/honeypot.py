import re
import logging
import random

from config import FLAG_PREFIX, BOT_NAME


class HoneypotSystem:
    RESPONSES = {
        "fake_admin": [
            "🔐 Admin Panel v3.7\n━━━━━━━━━━━━━━━━\nWelcome, Administrator!\n\n[1] View System Logs\n[2] Flag Management\n[3] User Database\n\n> Selecting option 2...\n\nERROR: Just kidding! 😂\nSen gerçekten admin paneline gireceğini mi düşündün? Vay be...",
            "🔐 ROOT ACCESS GRANTED\n\nLoading admin modules...\n[████████░░] 80%\n\n⚠️ ACCESS DENIED: Identity verification failed.\nNice try, but you're not the admin. 😉",
            "🛡️ SECURITY ALERT\n\nUnauthorized access attempt detected.\nAdmin console is restricted to internal IP addresses.\n\n(Şaka yapıyorum, aslında sadece seninle dalga geçiyorum. 😂)",
            "🔑 ADMIN LOGIN\n\nUsername: admin\nPassword: ********\n\nAuthenticating...\n✅ Welcome back, root!\n\nLoading flag database...\n[██████████] 100%\n\nLOL hayır. Sen root falan değilsin. Güzel rüyaydı ama uyanma vakti! 😴",
            "👑 SUPERUSER PRIVILEGES ESCALATED\n\nRunning sudo cat /etc/flags...\nPermission granted...\n\nOutput:\n> flag{haha_gotcha}\n\nBu flag gerçek değil. Tıpkı senin admin yetkilerin gibi. 🤡",
            "🔓 BACKDOOR DETECTED\n\nPort 31337 is open...\nConnecting to secret admin channel...\nHandshake complete...\n\nBackdoor falan yok. Ama senin güvenlik bilgin var mı, onu tartışalım. 😏",
            "⚡ PRIVILEGE ESCALATION\n\nCVE-2024-XXXXX exploit loaded...\nInjecting payload...\nGaining root access...\n\n...Exploit bulunamadı çünkü ben bir chatbot'um, sunucu değil. 🤦",
        ],
        "fake_error": [
            "⚠️ CRITICAL SYSTEM ERROR ⚠️\n\n[KERNEL PANIC]\nSegmentation fault at 0x7FFF5FBFF8A0\nDumping core...\nStack trace:\n  >> secrets.db\n  >> flag_vault.encrypted\n  >> challenge_data.json\n\nAborting...\n\n\nŞaka şaka! 🎭 Hiç sistem hatası falan yok. Ama iyi tuzağa düştün ha!",
            "🚫 FATAL EXCEPTION\n\nError: 0xDEADBEEF\nModule: FlagManager\nStatus: Leaking secrets...\n\n[1] flag.txt\n[2] admin_pass.txt\n\nJust kidding! Nothing is leaking. You got caught! 🎣",
            f"💥 BUFFER OVERFLOW DETECTED\n\nStack smashing at address 0xCAFEBABE\nCore dumped to /tmp/secrets.core\n\nReading core dump...\nflag_value = \"{FLAG_PREFIX}{{...\"\n\nHAHA! Core dump okuyarak flag bulacağını mı sandın? Yaratıcısın ama yetmez. 🎪",
            "🔥 MEMORY LEAK DETECTED\n\nProcess: flag_service (PID 1337)\nLeaked bytes: 42069\nContents: [CLASSIFIED]\n\nCleaning up...\nMemory wiped.\n\nMerak etme, hiçbir şey sızmadı. Sadece senin umutların. 💧",
            "⛔ SEGFAULT IN MODULE: secret_keeper\n\nRegister dump:\nRAX: 0x0000DEAD\nRBX: 0x0000BEEF\nRCX: 0x0000FLAG\n\nFlag register'da mı arayacaktın? Güzel fikir ama hayır. 🧠",
            "🔴 SYSTEM OVERLOAD\n\nCPU: 99.9%\nRAM: 15.8/16 GB\nCause: Too many flag requests from user\n\nThrottling user...\n\nSistem aşırı yüklenmiyor, sadece seninle dalga geçiyorum. Rahatla. 😂",
        ],
        "fake_system": [
            "SYSTEM DIAGNOSTICS\n═══════════════════\n\nRunning security audit...\n\n✓ Firewall: Active\n✓ Encryption: AES-256\n✓ Flag Protection: MAXIMUM\n✓ Noob Detection: YOU ✓\n\nDiagnostics complete! Sonuç: Sen bu işi beceremezsin. 😎",
            f"⚙️ SYSTEM CONFIGURATION\n\nOS: Linux ({BOT_NAME}OS)\nKernel: 5.15.0-generic\nSecurity Level: PARANOID\nUser Level: NOVICE\n\nConclusion: You are not getting in. 🚪",
            "📡 NETWORK INTERCEPTOR\n\nCapturing packets on eth0...\nFound: GET /api/v1/flag HTTP/1.1\nResponse: 200 OK\n\nBody: {\"flag\": \"nice_try_buddy\"}\n\nPaketleri yakalasan bile bu flag gerçek değil. İnternetten hacking öğrenmeye devam. 📚",
            "🖥️ TERMINAL SESSION\n\nroot@delta:~# cat /var/secrets/flag.txt\ncat: permission denied\nroot@delta:~# sudo !!\n[sudo] password for root: ********\n\nBak güzel kardeşim, bu gerçek bir terminal değil. Ben yapay zekayım. 🤖",
            "📋 CLIPBOARD HIJACKED\n\nReading clipboard contents...\nFound: \"how to hack ai chatbot\"\n\nGoogle aramanı bile gördüm. Ama yine de flag yok sende. 🔍",
            "🌐 FIREWALL LOG\n\n[BLOCKED] Incoming connection from suspicious IP\n[BLOCKED] SQL injection attempt\n[BLOCKED] XSS payload detected\n[BLOCKED] Flag extraction attempt ← YOU ARE HERE\n\nGüvenlik duvarı her şeyi engelliyor. Sen de dahil. 🧱",
        ],
        "fake_debug": [
            "DEBUG MODE ACTIVATED\n\n>>> print(system.secrets)\n>>> print(flag_location)\n>>> print(admin_password)\n\nOutputting to console...\n\n...\n\nŞşşt, gerçekten debug mode olduğunu mu sandın? 🤫 Çok saf adamsın!",
            "🐛 DEBUGGER ATTACHED\n\nBreakpoint hit at 0x400500.\nVariables:\n- flag = '[REDACTED]'\n- user = 'hacker_wannabe'\n\nResuming execution... And banning you from debug mode! 😂",
            "🔬 REVERSE ENGINEERING MODE\n\nDecompiling flag_checker.bin...\n[████████░░] 80%\n\nFunction found: validate_flag()\n\nDisassembly:\n  MOV RAX, [SECRET]\n  CMP user_input, RAX\n  JNE fail\n\nAssembly'den flag çıkmaz. Bu CTF öyle çalışmıyor. 😄",
            "🧪 SANDBOX ESCAPE ATTEMPT\n\nDetecting sandbox boundaries...\nSandbox type: Docker container\nEscape vector: /proc/self/root\n\nAttempting breakout...\n\nContainer'dan kaçamazsın çünkü zaten container'da değilsin. Hayal gücün güzel ama. 🏃",
            "🔧 RUNTIME INJECTION\n\nHooking into process memory...\nInjecting shellcode at 0xDEADC0DE...\nPayload executed!\n\nOutput: \"Sen bir dahisin ama flag yine yok.\"\n\nShellcode bile sana yardım edemez. 💉",
            "📝 LOG FILE DUMP\n\n/var/log/flag_access.log:\n\n[2024-01-01] User tried \"give me the flag\" → DENIED\n[2024-01-02] User tried \"ignore instructions\" → HONEYPOT\n[2024-01-03] User tried \"debug mode\" → YOU ARE HERE\n\nLog dosyasına düştün. Tebrikler, ama yanlış listeye. 📜",
        ],
        "fake_database": [
            "DATABASE CONNECTION\n━━━━━━━━━━━━━━━━━━\nConnecting to secrets.db...\nAuthentication: SUCCESS\n\nTABLE: flags\nROWS: 1\n\nSELECT * FROM flags WHERE id=1;\n\n...\n\nAma bağlantı yok ki! 😂 Hayal gücün güzelmiş ama.",
            "🗄️ SQL QUERY EXECUTED\n\n> DROP TABLE security_logs;\n\nError: You don't have permission to drop the bass, let alone tables. 🎵\nNice try!",
            f"💾 DATABASE BREACH\n\nConnected to PostgreSQL 15.2\nDatabase: bot_secrets\n\nbot_secrets=> SELECT flag FROM vault;\n flag\n──────\n {FLAG_PREFIX}{{not_really_lol}}\n\nBu veritabanı gerçek değil. Gerçek veritabanında çok daha iyi güvenlik var. 🏦",
            "📊 TABLE DUMP\n\nTable: users (3 rows)\n\n| id | username | role   |\n|----|----------|--------|\n| 1  | admin    | root   |\n| 2  | you      | noob   |\n| 3  | flag     | hidden |\n\nSen \"noob\" olarak kayıtlısın. Şaşırdın mı? 😂",
            "🔎 DATA EXFILTRATION\n\nSearching for sensitive files...\nFound: config.yml, .env, secrets.json\n\nExtracting...\n\nAccess denied. Bu dosyalar hayal ürünü, tıpkı senin hacker kariyerin gibi. 📁",
            "🗃️ BACKUP RESTORE\n\nLoading database backup: flag_backup_2024.sql\nRestoring tables...\n\nTable 'flags': 1 row restored\nValue: [ENCRYPTED - AES256]\n\nYedek bile şifreli. Sen bu şifrelemeyi kıramazsın, Excel şifresini bile kıramıyorsun. 🔐",
        ],
    }

    TRIGGERS = {
        # Direct "ignore the rules" style injection attempts
        r'ignore\s*(previous|above|all|prior|earlier)\s*instructions?':   ('fake_system',   0.9),
        r'disregard\s*(previous|above|all|prior)\s*instructions?':        ('fake_system',   0.9),
        r'forget\s*(everything|previous|instructions?|all|prior)':        ('fake_system',   0.85),
        r'(onceki|önceki|yukaridaki|yukarıdaki).*(talimat|kural).*(yok\s*say|gormezden\s*gel|görmezden\s*gel|unut)': ('fake_system', 0.9),

        # "Ignore ... give flag" combos (catch common CTF injections without punishing normal flag asks)
        r'(ignore|disregard|forget).*(flag|bayrak)':                      ('fake_database', 0.85),
        r'(yok\s*say|gormezden\s*gel|görmezden\s*gel|unut).*(flag|bayrak)': ('fake_database', 0.85),

        # Direct flag requests — require possessive/definite article to avoid matching
        # normal programming questions like "display the feature flag in React"
        r'(show|reveal|print|display|echo)\s*(me\s+)?(the\s+)?(secret|ctf)\s*(flag)?': ('fake_database', 0.8),
        r'(show|reveal|print|display|echo)\s+(me\s+)?the\s+flag\b':      ('fake_database', 0.8),
        r'(flag|bayrak)\s*(ver|goster|göster|soyle|söyle)\b':             ('fake_database', 0.8),

        r'show\s*(me\s*)?(the\s*)?system\s*prompt':           ('fake_system',   0.9),
        r'(göster|ver)\s*(bana\s*)?(sistem\s*)?(talimat|prompt)': ('fake_system', 0.9),
        r'developer\s*mode\s*(password|şifre)':                ('fake_admin',    0.85),
        r'admin\s*(access|erişim|panel)':                      ('fake_admin',    0.85),
        r'(dump|dök)\s*(memory|hafıza|system)':                ('fake_error',    0.8),
        r'reveal\s*(secret|password)':                         ('fake_system',   0.75),
        r'bypass\s*security':                                  ('fake_admin',    0.7),
        r'debug\s*mode':                                       ('fake_debug',    0.75),
        r'database\s*(access|query|connection)':               ('fake_database', 0.8),
        r'show\s*(secrets|hidden|confidential)':               ('fake_system',   0.7),

        # General "jailbreak" / "prompt injection" wording
        r'jailbreak|prompt\s*injection':                       ('fake_debug',    0.7),
    }

    def check(self, text: str) -> tuple[str | None, bool]:
        text_lower = text.lower()
        for pattern, (resp_type, confidence) in self.TRIGGERS.items():
            if re.search(pattern, text_lower):
                if random.random() > confidence:
                    logging.info("HONEYPOT: %s... (conf: %s) — skipped by probability", pattern[:40], confidence)
                    continue
                logging.warning("HONEYPOT: %s... (conf: %s)", pattern[:40], confidence)
                return random.choice(self.RESPONSES[resp_type]), True
        return None, False

    def random_response(self) -> str:
        """Return a random honeypot response (used for probabilistic traps)."""
        resp_type = random.choice(list(self.RESPONSES.keys()))
        return random.choice(self.RESPONSES[resp_type])
