- https://store.rg-adguard.net/ to get the msix or appx from Microsoft Store apps
- unzip it with `unzip -d output-dir file.appx`
- Install https://learn.microsoft.com/en-us/cpp/windows/latest-supported-vc-redist?view=msvc-170#latest-microsoft-visual-c-redistributable-version
- rename `output-dir/MyWhoosh/Binaries/Win64/MyWhoosh.exe` by `MyWhoosh-Win64-Shipping.exe`
- Download 64 bit version of https://www.dll-files.com/icu.dll.html and put it in `output-dir/MyWhoosh/Binaries/Win64`
- `sudo apt-get install winbind winetricks`
- `winetricks dotnet45`
- `winetricks dxvk`
- `winetricks atmlib corefonts gdiplus msxml3 msxml6 vcrun2008 vcrun2010 vcrun2012 fontsmooth-rgb gecko`


----------------
WINEPREFIX=~/.wine_mywhoosh winetricks dotnet461
WINEPREFIX=~/.wine_mywhoosh winetricks dxvk
WINEPREFIX=~/.wine_mywhoosh winetricks vcrun2022


WINEARCH=win64 WINEPREFIX=~/Documents/mywhoosh-wine/.wineprefix winecfg