# 繧ｻ繝・ヨ繧｢繝・・繧ｬ繧､繝・
---

## 繝ｪ繝昴ず繝医Μ蜿門ｾ・(git clone)
縺薙・繝ｪ繝昴ず繝医Μ縺ｯ縲∽ｻｮ諠ｳ迺ｰ蠅・ｼ・env・峨・菴懈・鬆・ｺ上↓繧医ｊ2騾壹ｊ縺ｮ騾ｲ繧∵婿縺後≠繧翫∪縺吶らｵ・ｹ斐・繝ｪ繧ｷ繝ｼ縺ｧ縲計env 繧貞・縺ｫ菴懈・縲阪′豎ゅａ繧峨ｌ繧句ｴ蜷医・繝代ち繝ｼ繝ｳB繧剃ｽｿ逕ｨ縺励※縺上□縺輔＞縲・
繝代ち繝ｼ繝ｳA: 蜈医↓ clone・井ｸ闊ｬ逧・↑豬√ｌ・・- HTTPS: `git clone https://github.com/atsu-oku/vmMigration.git`
- SSH: `git clone git@github.com:atsu-oku/vmMigration.git`
- 繝・ぅ繝ｬ繧ｯ繝医Μ縺ｸ遘ｻ蜍・ `cd vmMigration`

繝代ち繝ｼ繝ｳB: 蜈医↓ venv 繧剃ｽ懈・繝ｻ譛牙柑蛹悶＠縺ｦ縺九ｉ clone・医・繝ｪ繧ｷ繝ｼ貅匁侠・・- Windows/PowerShell・井ｾ具ｼ・  - `python -m venv .venv`
  - `.\\.venv\\Scripts\\Activate`
  - `git clone https://github.com/atsu-oku/vmMigration.git`
  - `cd vmMigration`
  - `pip install -r requirements.txt`
- macOS/Linux・・ash・・  - `python3 -m venv .venv`
  - `source .venv/bin/activate`
  - `git clone https://github.com/atsu-oku/vmMigration.git`
  - `cd vmMigration`
  - `pip install -r requirements.txt`

**蜑肴署譚｡莉ｶ**
- Python 3.11 莉･荳・- vCenter API 縺ｸ蛻ｰ驕泌庄閭ｽ縺ｪ繝阪ャ繝医Ρ繝ｼ繧ｯ迺ｰ蠅・- 蟇ｾ雎｡ VM 縺ｫ VMware Tools 縺悟ｰ主・繝ｻ遞ｼ蜒阪＠縲；uest Operations 縺悟茜逕ｨ蜿ｯ閭ｽ
- 繧ｲ繧ｹ繝・OS 縺ｫ `nmcli`・・etworkManager・峨′蛻ｩ逕ｨ蜿ｯ閭ｽ・井ｸｻ縺ｫ Linux・・
**繧､繝ｳ繧ｹ繝医・繝ｫ・域耳螂ｨ: 莉ｮ諠ｳ迺ｰ蠅・venv・・*
- 萓晏ｭ倥Δ繧ｸ繝･繝ｼ繝ｫ縺ｯ `requirements.txt` 縺ｧ邂｡逅・＠縺ｾ縺吶・- PowerShell (Windows)
  - `python -m venv .venv`
  - `.\\.venv\\Scripts\\Activate`
  - `python -m pip install --upgrade pip`
  - `pip install -r requirements.txt`
- bash (macOS/Linux)
  - `python3 -m venv .venv`
  - `source .venv/bin/activate`
  - `python -m pip install --upgrade pip`
  - `pip install -r requirements.txt`

**繧ｰ繝ｭ繝ｼ繝舌Ν迺ｰ蠅・〒縺ｮ繧ｻ繝・ヨ繧｢繝・・・・env 繧剃ｽｿ繧上↑縺・ｴ蜷茨ｼ・*
- 譌｢縺ｫ Python 繧偵げ繝ｭ繝ｼ繝舌Ν縺ｫ蟆主・縺励※縺・ｋ蝣ｴ蜷医〒繧ゅ∝庄閭ｽ縺ｪ繧峨Θ繝ｼ繧ｶ繝ｼ鬆伜沺縺ｫ繧､繝ｳ繧ｹ繝医・繝ｫ縺励※縺上□縺輔＞・域ｨｩ髯占｡晉ｪ√ｄ莉悶・繝ｭ繧ｸ繧ｧ繧ｯ繝医∈縺ｮ蠖ｱ髻ｿ繧帝∩縺代ｋ縺溘ａ・峨・- Windows・・owerShell・・  - Python 遒ｺ隱・ `python --version` 縺ｾ縺溘・ `py -3.11 --version`
  - 繝ｦ繝ｼ繧ｶ繝ｼ鬆伜沺縺ｸ蟆主・: `python -m pip install --user -r requirements.txt`
  - `--user` 縺ｧ蜈･繧後◆繧ｹ繧ｯ繝ｪ繝励ヨ縺ｯ騾壼ｸｸ `%USERPROFILE%\AppData\Roaming\Python\Python311\Scripts` 縺ｫ驟咲ｽｮ縺輔ｌ縺ｾ縺吶ょｿ・ｦ√↓蠢懊§縺ｦ PATH 縺ｫ霑ｽ蜉縺励※縺上□縺輔＞縲・  - 繧ｷ繧ｹ繝・Β蜈ｨ菴薙∈縺ｮ蟆主・縺悟ｿ・ｦ√↑蝣ｴ蜷医・邂｡逅・・ｨｩ髯舌・ PowerShell 縺ｧ螳溯｡後＠縺ｾ縺吶′縲∵耳螂ｨ縺ｯ縺励∪縺帙ｓ縲・- macOS/Linux・・ash・・  - Python 遒ｺ隱・ `python3 --version`
  - 繝ｦ繝ｼ繧ｶ繝ｼ鬆伜沺縺ｸ蟆主・: `python3 -m pip install --user -r requirements.txt`
  - 繝ｦ繝ｼ繧ｶ繝ｼ鬆伜沺縺ｮ螳溯｡後ヵ繧｡繧､繝ｫ縺ｯ騾壼ｸｸ `~/.local/bin` 縺ｫ驟咲ｽｮ縺輔ｌ縺ｾ縺吶１ATH 縺ｫ `~/.local/bin` 繧定ｿｽ蜉縺励※縺上□縺輔＞縲・- 蜍穂ｽ懃｢ｺ隱・  - `python -c "import pyVmomi, requests; print('deps ok')"`
- 豕ｨ諢冗せ
  - `sudo pip install` 縺ｯ讌ｵ蜉幃∩縺代※縺上□縺輔＞・医す繧ｹ繝・Β鬆伜沺繧呈ｱ壽沒縺励ｄ縺吶＞縺溘ａ・峨ゅｄ繧繧貞ｾ励↑縺・ｴ蜷医・豁｣遒ｺ縺ｪ隕∽ｻｶ縺ｮ繧ゅ→縺ｧ螳滓命縺励∝ｽｱ髻ｿ繧呈滑謠｡縺励※縺上□縺輔＞縲・  - 萓晏ｭ倩｡晉ｪ√′逋ｺ逕溘＠縺溷ｴ蜷医・莉ｮ諠ｳ迺ｰ蠅・ｼ・env・峨・菴ｿ逕ｨ縺ｫ蛻・ｊ譖ｿ縺医ｋ縺薙→繧呈耳螂ｨ縺励∪縺吶・
**險ｭ螳夲ｼ井ｻｻ諢擾ｼ・*
- 繝ｭ繧ｰ隧ｳ邏ｰ蠎ｦ: `VSPHERE_CLONE_LOG_LEVEL`
  - 豌ｸ邯夊ｨｭ螳・ `setx VSPHERE_CLONE_LOG_LEVEL DEBUG`・・owerShell縲よ眠縺励＞繧ｷ繧ｧ繝ｫ縺ｧ譛牙柑・・  - 荳譎りｨｭ螳・ `set VSPHERE_CLONE_LOG_LEVEL=DEBUG`・・owerShell・・/ `export VSPHERE_CLONE_LOG_LEVEL=DEBUG`・・ash・・
**螳溯｡・*
- `python cloneAndVmotion.py`
- 繧ｹ繧ｯ繝ｪ繝励ヨ螳溯｡御ｸｭ縺ｫ縲√た繝ｼ繧ｹ/螳帛・ vCenter 縺ｮ隱崎ｨｼ諠・ｱ縲∝ｯｾ雎｡ VM 蜷阪√ご繧ｹ繝・OS 隱崎ｨｼ諠・ｱ繧貞・蜉帙＠縺ｾ縺吶・- 蜷・ヵ繧ｧ繝ｼ繧ｺ縺ｧ遒ｺ隱阪・繝ｭ繝ｳ繝励ヨ縺瑚｡ｨ遉ｺ縺輔ｌ縲～y` 縺ｧ邯夊｡後＠縺ｾ縺吶・
**繝医Λ繝悶Ν繧ｷ繝･繝ｼ繝・ぅ繝ｳ繧ｰ**
- 逍朱壼､ｱ謨・ 繝阪ャ繝医Ρ繝ｼ繧ｯ繝昴Μ繧ｷ繝ｼ/繝輔ぃ繧､繧｢繧ｦ繧ｩ繝ｼ繝ｫ縺ｧ ICMP 縺碁・譁ｭ縺輔ｌ縺ｦ縺・↑縺・°遒ｺ隱・- 隱崎ｨｼ螟ｱ謨・ VMware Tools 蛛ｴ縺ｧ蟇ｾ雎｡繧｢繧ｫ繧ｦ繝ｳ繝医↓ Guest Operations 險ｱ蜿ｯ縺後≠繧九°遒ｺ隱・- 隧ｳ邏ｰ繝ｭ繧ｰ: `VSPHERE_CLONE_LOG_LEVEL=DEBUG` 繧定ｨｭ螳・
**陬懆ｶｳ**
- `requirement.txt` 繧ょ酔譴ｱ縺励※縺・∪縺吶′縲・壼ｸｸ縺ｯ `requirements.txt` 縺ｮ菴ｿ逕ｨ繧呈耳螂ｨ縺励∪縺吶・
