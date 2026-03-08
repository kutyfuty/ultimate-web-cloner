@echo off
chcp 65001 >nul
color 0E
echo =======================================================
echo 🧙‍♂️ Büyucu Motoru (Web Cloner) Otomatik EXE Derleyici 🧙‍♂️
echo =======================================================
echo.
echo Bu kucuk sihir, yazilan Python kodlarini toplayacak
echo ve herkesin cift tiklayip acabilecegi bir EXE yapacak.
echo Her yeni ozellik eklendiginde sadece bu dosyaya cift 
echo tiklamaniz yeterlidir!
echo.
echo [1/3] Python kutuphaneleri (Gereksinimler) kontrol ediliyor...
python -m pip install pyinstaller playwright playwright-stealth PyQt6 beautifulsoup4 requests >nul 2>&1

echo.
echo [2/3] Gizli tarayici motorlari (Chromium) indiriliyor...
:: Gizli ayar: PLAYWRIGHT_BROWSERS_PATH=0 yapmazsak tarayıcıyı C:\Users\xxx\.cache içine kurar ve EXE'ye aktarmaz.
:: 0 yaptığımızda direkt kendi içine kurar ve EXE bunu alabilir.
set PLAYWRIGHT_BROWSERS_PATH=0
python -m playwright install chromium >nul 2>&1

echo.
echo [3/3] Kodlar Efsanevi bir EXE'ye donusturuluyor... 
echo Lutfen bekleyin, bu islem yaklasik 2-3 dakika surebilir...
echo.

:: Calisan eski EXE varsa kapat ki PermissionError vermesin
taskkill /F /IM WebCloner_GodMode.exe >nul 2>&1

:: --noconfirm : dist klasoru varsa sorusuz ustune yazar (Guncelleme mantigi)
:: --noconsole : Arkada siyah komut penceresi acilmasini engeller
:: --collect-all playwright : Playwright motorunu EXE icine dogru sekilde gomer
:: --collect-all playwright_stealth : JS dosyalarının (magic.arrays.js vb) bulunamama hatasını onler
python -m PyInstaller --noconfirm --noconsole --name "WebCloner_GodMode" --collect-all playwright --collect-all playwright_stealth --hidden-import PyQt6 main.py

echo.
color 0A
echo =======================================================
echo ✅ ISLEM BASARIYLA TAMAMLANDI! 
echo ✅ Yeni guncel programiniza su klasorden ulasabilirsiniz:
echo    👉 dist\WebCloner_GodMode\WebCloner_GodMode.exe
echo =======================================================
pause
