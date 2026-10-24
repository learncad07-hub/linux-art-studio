Name:           linux-art-studio
Version:        0.1.0
Release:        1%{?dist}
Summary:        Guided installer for creative and game-dev tools on Fedora

License:        GPL-3.0-or-later
URL:            https://github.com/learncad07-hub/linux-art-studio
Source0:        %{name}-%{version}.tar.gz

BuildArch:      noarch

Requires:       python3-gobject
Requires:       gtk4
Requires:       libadwaita
Requires:       polkit
Requires:       dnf
Requires:       flatpak
Requires:       unzip

%description
Linux Art Studio is a guided, live-logged installer for creative and
game-dev tools that don't have good native Fedora packages, including
Unreal Engine, DaVinci Resolve, and Blender, plus a catalog of other
creative tools available through Flatpak.

%prep
%setup -q

%build
# Pure Python/data, nothing to compile.

%install
rm -rf %{buildroot}

install -d %{buildroot}%{_datadir}/%{name}
cp -a src/app %{buildroot}%{_datadir}/%{name}/
cp -a src/backend %{buildroot}%{_datadir}/%{name}/
cp -a src/data %{buildroot}%{_datadir}/%{name}/
cp -a src/helper %{buildroot}%{_datadir}/%{name}/
find %{buildroot}%{_datadir}/%{name} -name '__pycache__' -type d -exec rm -rf {} +

install -d %{buildroot}%{_bindir}
install -m 755 packaging/linux-art-studio %{buildroot}%{_bindir}/linux-art-studio

install -d %{buildroot}%{_datadir}/applications
install -m 644 packaging/linux-art-studio.desktop %{buildroot}%{_datadir}/applications/%{name}.desktop

install -d %{buildroot}%{_datadir}/polkit-1/actions
install -m 644 data/polkit/local.creativeinstaller.helper.policy \
    %{buildroot}%{_datadir}/polkit-1/actions/local.creativeinstaller.helper.policy

install -d %{buildroot}%{_datadir}/icons/hicolor/scalable/apps
install -m 644 src/app/assets/app-icon.svg \
    %{buildroot}%{_datadir}/icons/hicolor/scalable/apps/%{name}.svg

%post
/usr/bin/gtk-update-icon-cache -q -t -f %{_datadir}/icons/hicolor &>/dev/null || :
/usr/bin/update-desktop-database -q %{_datadir}/applications &>/dev/null || :

%postun
/usr/bin/gtk-update-icon-cache -q -t -f %{_datadir}/icons/hicolor &>/dev/null || :
/usr/bin/update-desktop-database -q %{_datadir}/applications &>/dev/null || :

%files
%license LICENSE
%doc README.md
%{_bindir}/linux-art-studio
%{_datadir}/%{name}/
%{_datadir}/applications/%{name}.desktop
%{_datadir}/polkit-1/actions/local.creativeinstaller.helper.policy
%{_datadir}/icons/hicolor/scalable/apps/%{name}.svg

%changelog
* Sat Oct 24 2026 vrm <learncad07-hub@users.noreply.github.com> - 0.1.0-1
- First packaged release: Unreal Engine, DaVinci Resolve, Blender, and
  the Flatpak catalog.
