# vSphere 閾ｪ蜍募喧繧ｹ繧ｯ繝ｪ繝励ヨ

`cloneAndVmotion.py` 縺ｯ縲」Sphere 荳翫〒 STG 迺ｰ蠅・・ VM 繧偵け繝ｭ繝ｼ繝ｳ縺励∝ｮ帛・ vCenter/繝阪ャ繝医Ρ繝ｼ繧ｯ縺ｸ逋ｻ骭ｲ繝ｻ蜀肴ｧ区・縺励◆縺・∴縺ｧ縲￣RD 逕ｨ繝・・繧ｿ繧ｹ繝医い縺ｸ Storage vMotion 縺ｧ遘ｻ陦後☆繧倶ｽ懈･ｭ繧定・蜍募喧縺吶ｋ繧ｹ繧ｯ繝ｪ繝励ヨ縺ｧ縺吶ゅご繧ｹ繝・OS 縺ｮ繝阪ャ繝医Ρ繝ｼ繧ｯ險ｭ螳壹・ `nmcli` 繧堤畑縺・※閾ｪ蜍暮←逕ｨ繝ｻ讀懆ｨｼ縺励∪縺吶・
---

## 繧ｯ繧､繝・け繧ｹ繧ｿ繝ｼ繝茨ｼ域耳螂ｨ: venv 竊・clone・・- Windows/PowerShell
  - `python -m venv .venv`
  - `.\\.venv\\Scripts\\Activate`
  - `git clone https://github.com/atsu-oku/vmMigration.git`
  - `cd vmMigration`
  - `python -m pip install --upgrade pip`
  - `pip install -r requirements.txt`
  - `python cloneAndVmotion.py`
- macOS/Linux・・ash・・  - `python3 -m venv .venv`
  - `source .venv/bin/activate`
  - `git clone https://github.com/atsu-oku/vmMigration.git`
  - `cd vmMigration`
  - `python -m pip install --upgrade pip`
  - `pip install -r requirements.txt`
  - `python cloneAndVmotion.py`

莉｣譖ｿ縺ｮ謇矩・ｼ・lone 竊・venv・峨・ `SETUP.md` 縺ｫ險倩ｼ峨＠縺ｦ縺・∪縺吶・
---

## 迚ｹ髟ｷ
- 繧ｯ繝ｭ繝ｼ繝ｳ 竊・螳帛・ vCenter 逋ｻ骭ｲ 竊・NIC 蜀肴ｧ区・ 竊・IP/GW/DNS/繝ｫ繝ｼ繝磯←逕ｨ 竊・Storage vMotion 縺ｾ縺ｧ閾ｪ蜍募喧
- 險ｭ螳壼ｾ後・ `nmcli` 縺ｧ讀懆ｨｼ縺励∽ｸ肴紛蜷医′縺ゅｌ縺ｰ隴ｦ蜻・- 繧ｨ繝ｩ繝ｼ譎ゅ・繝ｭ繝ｼ繝ｫ繝舌ャ繧ｯ・医け繝ｭ繝ｼ繝ｳ蜑企勁繝ｻ荳崎ｦ√ヵ繧｡繧､繝ｫ蜑企勁 縺ｪ縺ｩ・峨ｒ謠先｡・
## 蟇ｾ蠢懃腸蠅・・蜑肴署
- vCenter 縺ｫ API 蛻ｰ驕斐〒縺阪ｋ縺薙→
- 蟇ｾ雎｡ VM 縺ｫ VMware Tools 縺悟ｰ主・繝ｻ遞ｼ蜒阪＠縲；uest Operations 縺悟茜逕ｨ蜿ｯ閭ｽ
- 繧ｲ繧ｹ繝・OS 蛛ｴ縺ｧ `nmcli`・・etworkManager・峨′蛻ｩ逕ｨ蜿ｯ閭ｽ・井ｸｻ縺ｫ Linux・・- Python 3.11 莉･髯・
## 萓晏ｭ倥Δ繧ｸ繝･繝ｼ繝ｫ
- pyvmomi
- requests
- 隧ｳ邏ｰ縺ｯ `requirements.txt` 縺ｨ `SETUP.md` 繧貞盾辣ｧ

## 螳溯｡後ヵ繝ｭ繝ｼ・域ｦりｦ・ｼ・1. 繧ｽ繝ｼ繧ｹ vCenter 縺九ｉ蟇ｾ雎｡ VM 縺ｮ NIC 諠・ｱ縲；W縲．NS 縺ｪ縺ｩ繧貞叙蠕・2. 荳譎ゅョ繝ｼ繧ｿ繧ｹ繝医い縺ｸ繧ｯ繝ｭ繝ｼ繝ｳ菴懈・・医け繝ｭ繝ｼ繝ｳ蛛ｴ NIC 繧貞・譛溷喧/隱ｿ謨ｴ・・3. 螳帛・ vCenter 縺ｸ逋ｻ骭ｲ縺励￣RD 繝阪ャ繝医Ρ繝ｼ繧ｯ縺ｫ蜷医ｏ縺帙※ NIC 蜀肴ｧ区・
4. 繧ｲ繧ｹ繝・OS 蛛ｴ縺ｧ `nmcli` 縺ｫ繧医ｊ IP/GW/DNS/繝ｫ繝ｼ繝医ｒ驕ｩ逕ｨ繝ｻ讀懆ｨｼ
5. 荳譎る・鄂ｮ縺九ｉ譛邨・PRD 逕ｨ繝・・繧ｿ繧ｹ繝医い縺ｸ Storage vMotion 縺ｧ遘ｻ陦・
## 菴ｿ縺・婿・亥ｯｾ隧ｱ縺ｮ豬√ｌ・・- 螳溯｡・ `python cloneAndVmotion.py`
- 蟇ｾ隧ｱ縺ｧ莉･荳九ｒ蜈･蜉・  - 繧ｽ繝ｼ繧ｹ/螳帛・ vCenter 縺ｮ隱崎ｨｼ諠・ｱ
  - 遘ｻ陦悟ｯｾ雎｡ VM 蜷・  - 繧ｲ繧ｹ繝・OS 縺ｮ隱崎ｨｼ諠・ｱ・・oot 縺ｾ縺溘・ sudo 蜿ｯ閭ｽ縺ｪ admin・・- 蜷・ヵ繧ｧ繝ｼ繧ｺ縺ｧ遒ｺ隱阪Γ繝・そ繝ｼ繧ｸ縺瑚｡ｨ遉ｺ縺輔ｌ縲～y` 縺ｧ邯夊｡・
## 繝医Λ繝悶Ν繧ｷ繝･繝ｼ繝・ぅ繝ｳ繧ｰ
- 逍朱壼､ｱ謨・ 繝阪ャ繝医Ρ繝ｼ繧ｯ繝昴Μ繧ｷ繝ｼ/繝輔ぃ繧､繧｢繧ｦ繧ｩ繝ｼ繝ｫ縺ｧ ICMP 縺碁・譁ｭ縺輔ｌ縺ｦ縺・↑縺・°遒ｺ隱・- 隱崎ｨｼ螟ｱ謨・ VMware Tools 蛛ｴ縺ｧ蟇ｾ雎｡繧｢繧ｫ繧ｦ繝ｳ繝医↓ Guest Operations 讓ｩ髯舌′縺ゅｋ縺狗｢ｺ隱・- 隧ｳ邏ｰ繝ｭ繧ｰ: `VSPHERE_CLONE_LOG_LEVEL=DEBUG` 繧定ｨｭ螳・
## 髢狗匱繝｡繝｢
- 萓晏ｭ倥・ `requirements.txt` 縺ｫ險倩ｼ会ｼ・pip install -r requirements.txt`・・- 隧ｳ邏ｰ縺ｪ繧ｻ繝・ヨ繧｢繝・・縺ｯ `SETUP.md` 繧貞盾辣ｧ
- Issue/PR 縺ｫ繧医ｋ謾ｹ蝟・署譯医ｒ豁楢ｿ・

