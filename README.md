# vSphere 閾ｪ蜍募喧繧ｹ繧ｯ繝ｪ繝励ヨ

`gptCloneAndVmotion.py` 縺ｯ縲」Sphere 荳翫〒 STG 迺ｰ蠅・・ VM 繧偵け繝ｭ繝ｼ繝ｳ縺励∝ｮ帛・ vCenter/繝阪ャ繝医Ρ繝ｼ繧ｯ縺ｸ逋ｻ骭ｲ繝ｻ蜀肴ｧ区・縺励◆縺・∴縺ｧ縲￣RD 逕ｨ繝・・繧ｿ繧ｹ繝医い縺ｸ Storage vMotion 縺ｧ遘ｻ陦後☆繧倶ｽ懈･ｭ繧定・蜍募喧縺吶ｋ繧ｹ繧ｯ繝ｪ繝励ヨ縺ｧ縺吶ゅご繧ｹ繝・OS 縺ｮ繝阪ャ繝医Ρ繝ｼ繧ｯ險ｭ螳壹・ `nmcli` 繧堤畑縺・※閾ｪ蜍暮←逕ｨ繝ｻ讀懆ｨｼ縺励∪縺吶・
---

##  繝ｪ繝昴ず繝医Μ蜿門ｾ・(git clone)
- HTTPS: `git clone https://github.com/atsu-oku/vmMigration.git`
- SSH: `git clone git@github.com:atsu-oku/vmMigration.git`
- 繝・ぅ繝ｬ繧ｯ繝医Μ縺ｸ遘ｻ蜍・ `cd vmMigration`

**迚ｹ髟ｷ**
- 繧ｯ繝ｭ繝ｼ繝ｳ菴懈・ 竊・螳帛・ vCenter 逋ｻ骭ｲ 竊・NIC 蜀肴ｧ区・ 竊・IP/GW/DNS/繝ｫ繝ｼ繝磯←逕ｨ 竊・Storage vMotion 縺ｾ縺ｧ縺ｮ豬√ｌ繧定・蜍募喧
- 險ｭ螳壼ｾ後・ `nmcli` 縺ｧ讀懆ｨｼ縺励・ｽ滄ｽｬ縺後≠繧後・隴ｦ蜻・- 繧ｨ繝ｩ繝ｼ譎ゅ・繝ｭ繝ｼ繝ｫ繝舌ャ繧ｯ・医け繝ｭ繝ｼ繝ｳ蜑企勁繝ｻ荳崎ｦ√ヵ繧｡繧､繝ｫ蜑企勁・峨ｒ譯亥・

**蟇ｾ蠢・蜑肴署**
- vCenter 縺ｫ API 蛻ｰ驕斐〒縺阪ｋ縺薙→
- 蟇ｾ雎｡ VM 縺ｫ VMware Tools 縺悟ｰ主・繝ｻ遞ｼ蜒阪＠縲；uest Operations 縺悟茜逕ｨ蜿ｯ閭ｽ
- 繧ｲ繧ｹ繝・OS 蛛ｴ縺ｫ `nmcli`・・etworkManager・峨′蛻ｩ逕ｨ蜿ｯ閭ｽ縺ｧ縺ゅｋ縺薙→・井ｸｻ縺ｫ Linux・・- Python 3.11 莉･荳・
**萓晏ｭ倥Δ繧ｸ繝･繝ｼ繝ｫ**
- pyvmomi
- requests
- 蟆主・譁ｹ豕輔・ `requirements.txt` 繧貞盾辣ｧ・医そ繝・ヨ繧｢繝・・隧ｳ邏ｰ縺ｯ `SETUP.md`・・
---

## 1. 繧､繝ｳ繧ｹ繝医・繝ｫ・医け繧､繝・け・・- Windows/PowerShell 縺ｮ萓・  - `python -m venv .venv`
  - `.\\.venv\\Scripts\\Activate`
  - `python -m pip install --upgrade pip`
  - `pip install -r requirements.txt`
- 螳溯｡・  - `python gptCloneAndVmotion.py`
- 繧ｰ繝ｭ繝ｼ繝舌Ν迺ｰ蠅・〒縺ｮ蟆主・繧・macOS/Linux 縺ｮ謇矩・・ `SETUP.md` 繧貞盾辣ｧ縺励※縺上□縺輔＞縲・
## 2. 荳ｻ縺ｪ險ｭ螳壼､・医せ繧ｯ繝ｪ繝励ヨ蜀・ｼ・莉･荳九・ `gptCloneAndVmotion.py` 蜀帝ｭ縺ｮ螳壽焚縺ｧ謖・ｮ壹＠縺ｾ縺吶ょ､縺ｯ迺ｰ蠅・↓蜷医ｏ縺帙※邱ｨ髮・＠縺ｦ縺上□縺輔＞縲・- vCenter 謗･邯・  - `VCSA_HOST_SOURCE`: 繧ｽ繝ｼ繧ｹ vCenter 縺ｮ FQDN/IP
  - `VCSA_HOST_DEST`: 螳帛・ vCenter 縺ｮ FQDN/IP
  - `VCSA_USER`: 謗･邯壹Θ繝ｼ繧ｶ繝ｼ・井ｾ・ `administrator@vsphere.local`・・  - `VCSA_PORT`: 443・域里螳夲ｼ・- 繝ｪ繧ｽ繝ｼ繧ｹ/驟咲ｽｮ
  - `TARGET_DATASTORE_NAME`: 繧ｯ繝ｭ繝ｼ繝ｳ縺ｮ荳譎る・鄂ｮ蜈医ョ繝ｼ繧ｿ繧ｹ繝医い・・TG 蛛ｴ・・  - `TARGET_DATASTORE_NAME_FINAL`: 譛邨る・鄂ｮ蜈医・ PRD 逕ｨ繝・・繧ｿ繧ｹ繝医い
  - `TARGET_CLUSTER_NAME`: 螳帛・縺ｮ繧ｯ繝ｩ繧ｹ繧ｿ蜷・- 繧ｲ繧ｹ繝・OS 隱崎ｨｼ
  - `GUEST_ROOT_USER` / `GUEST_ADMIN_USER`
  - 繝代せ繝ｯ繝ｼ繝峨・螳溯｡梧凾縺ｫ蟇ｾ隧ｱ蜈･蜉幢ｼ医せ繧ｯ繝ｪ繝励ヨ縺ｫ蟷ｳ譁・ｿ晏ｭ倥＠縺ｾ縺帙ｓ・・- 繝ｭ繧ｰ隧ｳ邏ｰ蠎ｦ・育腸蠅・､画焚・・  - `VSPHERE_CLONE_LOG_LEVEL`・井ｾ・ `INFO`/`DEBUG`・・
## 3. 螳溯｡後ヵ繝ｭ繝ｼ・域ｦりｦ・ｼ・1. 繧ｽ繝ｼ繧ｹ vCenter 縺九ｉ蟇ｾ雎｡ VM 縺ｮ NIC 諠・ｱ縲；W縲．NS 縺ｪ縺ｩ繧貞叙蠕・2. 繝・・繧ｿ繧ｹ繝医い縺ｸ繧ｯ繝ｭ繝ｼ繝ｳ菴懈・・医け繝ｭ繝ｼ繝ｳ蛛ｴ NIC 繧貞・譛溷喧/隱ｿ謨ｴ・・3. 螳帛・ vCenter 縺ｸ逋ｻ骭ｲ縺励￣RD 繝阪ャ繝医Ρ繝ｼ繧ｯ縺ｫ蜷医ｏ縺帙※ NIC 蜀肴ｧ区・
4. 繧ｲ繧ｹ繝・OS 蜀・〒 `nmcli` 縺ｫ繧医ｊ IP/GW/DNS/繝ｫ繝ｼ繝医ｒ驕ｩ逕ｨ繝ｻ讀懆ｨｼ
5. 荳譎る・鄂ｮ縺九ｉ譛邨・PRD 逕ｨ繝・・繧ｿ繧ｹ繝医い縺ｸ Storage vMotion 縺ｧ遘ｻ陦・
## 4. 菴ｿ縺・婿・亥ｯｾ隧ｱ縺ｮ豬√ｌ・・- 螳溯｡・ `python gptCloneAndVmotion.py`
- 蟇ｾ隧ｱ縺ｧ莉･荳九ｒ鬆・↓蜈･蜉・  - 繧ｽ繝ｼ繧ｹ/螳帛・ vCenter 縺ｮ隱崎ｨｼ諠・ｱ
  - 遘ｻ陦悟ｯｾ雎｡ VM 蜷・  - 繧ｲ繧ｹ繝・OS 縺ｮ隱崎ｨｼ諠・ｱ・・oot 縺ｾ縺溘・ sudo 蜿ｯ閭ｽ縺ｪ admin・・- 蜷・ヵ繧ｧ繝ｼ繧ｺ縺ｧ遒ｺ隱阪Γ繝・そ繝ｼ繧ｸ縺瑚｡ｨ遉ｺ縺輔ｌ縲～y` 縺ｧ邯夊｡後＠縺ｾ縺吶・
## 5. 繧医￥縺ゅｋ雉ｪ蝠擾ｼ・AQ・・- Q. Windows 縺ｮ繧ｲ繧ｹ繝医〒繧ょ虚縺阪∪縺吶°・・  - A. 縺・＞縺医ゅロ繝・ヨ繝ｯ繝ｼ繧ｯ險ｭ螳壹↓ `nmcli` 繧堤畑縺・ｋ縺溘ａ縲´inux・・etworkManager 蛻ｩ逕ｨ・峨ｒ諠ｳ螳壹＠縺ｦ縺・∪縺吶・- Q. 繝ｫ繝ｼ繝・ぅ繝ｳ繧ｰ縺ｯ縺ｩ縺ｮ繧医≧縺ｫ驕ｩ逕ｨ縺輔ｌ縺ｾ縺吶°・・  - A. 繧ｲ繝ｼ繝医え繧ｧ繧､縺ｮ繧ｻ繧ｰ繝｡繝ｳ繝医→邏舌▼縺・NIC 縺ｫ蟇ｾ縺励※繧ｹ繧ｿ繝・ぅ繝・け繝ｫ繝ｼ繝医ｒ驕ｩ逕ｨ縺励・←逕ｨ蠕後↓讀懆ｨｼ縺励∪縺吶・- Q. 騾比ｸｭ縺ｧ螟ｱ謨励＠縺溷ｴ蜷医・・・  - A. 蜿ｯ閭ｽ縺ｪ遽・峇縺ｧ繧ｯ繝ｭ繝ｼ繝ｳ VM 縺ｮ蜑企勁繧・ｸ崎ｦ√ヵ繧｡繧､繝ｫ蜑企勁縺ｪ縺ｩ縺ｮ繝ｭ繝ｼ繝ｫ繝舌ャ繧ｯ謇矩・ｒ譯亥・縺励∪縺吶・
## 6. 繝医Λ繝悶Ν繧ｷ繝･繝ｼ繝・ぅ繝ｳ繧ｰ
- 逍朱壹′螟ｱ謨励☆繧・  - 繝阪ャ繝医Ρ繝ｼ繧ｯ繝昴Μ繧ｷ繝ｼ/繝輔ぃ繧､繧｢繧ｦ繧ｩ繝ｼ繝ｫ縺ｧ ICMP 縺碁・譁ｭ縺輔ｌ縺ｦ縺・↑縺・°遒ｺ隱・  - 螳帛・繝阪ャ繝医Ρ繝ｼ繧ｯ縺ｮ VLAN/繧ｻ繧ｰ繝｡繝ｳ繝郁ｨｭ螳壹ｒ遒ｺ隱・- 繧ｲ繧ｹ繝・OS 隱崎ｨｼ縺ｫ螟ｱ謨励☆繧・  - VMware Tools 縺ｧ隧ｲ蠖薙い繧ｫ繧ｦ繝ｳ繝医↓ Guest Operations 縺ｸ縺ｮ繧｢繧ｯ繧ｻ繧ｹ讓ｩ縺後≠繧九°遒ｺ隱・  - sudo 險ｭ螳夲ｼ医ヱ繧ｹ繝ｯ繝ｼ繝芽ｦ∵ｱ・蜈埼勁・峨ｒ遒ｺ隱・- 隧ｳ邏ｰ繝ｭ繧ｰ繧定ｦ九◆縺・  - 螳溯｡悟燕縺ｫ `VSPHERE_CLONE_LOG_LEVEL=DEBUG` 繧定ｨｭ螳・
## 7. 髢狗匱繝｡繝｢
- 萓晏ｭ倥・ `requirements.txt` 縺ｫ險倩ｼ会ｼ・pip install -r requirements.txt`・・- 隧ｳ邏ｰ縺ｪ繧ｻ繝・ヨ繧｢繝・・縺ｯ `SETUP.md` 繧貞盾辣ｧ
- Issue/PR 縺ｫ繧医ｋ謾ｹ蝟・署譯医ｒ豁楢ｿ弱＠縺ｾ縺・
