#!/usr/bin/env python3
"""
Retrieves direct download links for Microsoft Store apps.

Queries Microsoft's ClientWebService and DisplayCatalog APIs to generate
direct download URLs for .appx, .msix, .appxbundle, and .msixbundle files
without relying on third-party services like store.rg-adguard.net.
Inspired by https://github.com/1NobleCyber/Get-MSStoreDownloadLinks
Itself sourced from https://github.com/MattiasC85/Scripts/blob/master/OSD

Usage:
    python get_msstore_download_links.py <ProductId> [--architecture {x86,x64,arm,arm64,neutral}]
                                                     [--locale LOCALE]
                                                     [--os-sku-id OS_SKU_ID]
                                                     [--os-version OS_VERSION]

Examples:
    python get_msstore_download_links.py 9nbhcs1lx4r0 --architecture x64
    python get_msstore_download_links.py 9ndh0f2vhzx2
"""

import argparse
import base64
import hashlib
import json
import os
import pathlib
import platform
import random
import sys
import zipfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable, Dict, List, Optional, Tuple
from xml.etree import ElementTree as ET

import ssl
import urllib.request
import urllib.error

_SSL_CTX = ssl._create_unverified_context()

_MACHINE_TO_ARCH = {
    "x86_64": "x64",
    "amd64": "x64",
    "i386": "x86",
    "i686": "x86",
    "aarch64": "arm64",
    "arm64": "arm64",
    "armv7l": "arm",
    "armv6l": "arm",
}

def _current_arch() -> str:
    """Return the MS Store architecture name for the current machine."""
    return _MACHINE_TO_ARCH.get(platform.machine().lower(), "x64")


INSTALLED_NON_LEAF_UPDATE_IDS = [
    1, 2, 3, 11, 19, 544, 549, 2359974, 2359977, 5169044, 8788830,
    23110993, 23110994, 54341900, 54343656, 59830006, 59830007, 59830008,
    60484010, 62450018, 62450019, 62450020, 66027979, 66053150, 97657898,
    98822896, 98959022, 98959023, 98959024, 98959025, 98959026, 104433538,
    104900364, 105489019, 117765322, 129905029, 130040031, 132387090,
    132393049, 133399034, 138537048, 140377312, 143747671, 158941041,
    158941042, 158941043, 158941044, 159123858, 159130928, 164836897,
    164847386, 164848327, 164852241, 164852246, 164852252, 164852253,
]

OTHER_CACHED_UPDATE_IDS = [
    10, 17, 2359977, 5143990, 5169043, 5169047, 8806526, 9125350, 9154769,
    10809856, 23110995, 23110996, 23110999, 23111000, 23111001, 23111002,
    23111003, 23111004, 24513870, 28880263, 30077688, 30486944, 30526991,
    30528442, 30530496, 30530501, 30530504, 30530962, 30535326, 30536242,
    30539913, 30545142, 30545145, 30545488, 30546212, 30547779, 30548797,
    30548860, 30549262, 30551160, 30551161, 30551164, 30553016, 30553744,
    30554014, 30559008, 30559011, 30560006, 30560011, 30561006, 30563261,
    30565215, 30578059, 30664998, 30677904, 30681618, 30682195, 30685055,
    30702579, 30708772, 30709591, 30711304, 30715418, 30720106, 30720273,
    30732075, 30866952, 30866964, 30870749, 30877852, 30878437, 30890151,
    30892149, 30990917, 31049444, 31190936, 31196961, 31197811, 31198836,
    31202713, 31203522, 31205442, 31205557, 31207585, 31208440, 31208451,
    31209591, 31210536, 31211625, 31212713, 31213588, 31218518, 31219420,
    31220279, 31220302, 31222086, 31227080, 31229030, 31238236, 31254198,
    31258008, 36436779, 36437850, 36464012, 41916569, 47249982, 47283134,
    58577027, 58578040, 58578041, 58628920, 59107045, 59125697, 59142249,
    60466586, 60478936, 66450441, 66467021, 66479051, 75202978, 77436021,
    77449129, 85159569, 90199702, 90212090, 96911147, 97110308, 98528428,
    98665206, 98837995, 98842922, 98842977, 98846632, 98866485, 98874250,
    98879075, 98904649, 98918872, 98945691, 98959458, 98984707, 100220125,
    100238731, 100662329, 100795834, 100862457, 103124811, 103348671,
    104369981, 104372472, 104385324, 104465831, 104465834, 104467697,
    104473368, 104482267, 104505005, 104523840, 104550085, 104558084,
    104659441, 104659675, 104664678, 104668274, 104671092, 104673242,
    104674239, 104679268, 104686047, 104698649, 104751469, 104752478,
    104755145, 104761158, 104762266, 104786484, 104853747, 104873258,
    104983051, 105063056, 105116588, 105178523, 105318602, 105362613,
    105364552, 105368563, 105369591, 105370746, 105373503, 105373615,
    105376634, 105377546, 105378752, 105379574, 105381626, 105382587,
    105425313, 105495146, 105862607, 105939029, 105995585, 106017178,
    106129726, 106768485, 107825194, 111906429, 115121473, 115578654,
    116630363, 117835105, 117850671, 118638500, 118662027, 118872681,
    118873829, 118879289, 118889092, 119501720, 119551648, 119569538,
    119640702, 119667998, 119674103, 119697201, 119706266, 119744627,
    119773746, 120072697, 120144309, 120214154, 120357027, 120392612,
    120399120, 120553945, 120783545, 120797092, 120881676, 120889689,
    120999554, 121168608, 121268830, 121341838, 121729951, 121803677,
    122165810, 125408034, 127293130, 127566683, 127762067, 127861893,
    128571722, 128647535, 128698922, 128701748, 128771507, 129037212,
    129079800, 129175415, 129317272, 129319665, 129365668, 129378095,
    129424803, 129590730, 129603714, 129625954, 129692391, 129714980,
    129721097, 129886397, 129968371, 129972243, 130009862, 130033651,
    130040030, 130040032, 130040033, 130091954, 130100640, 130131267,
    130131921, 130144837, 130171030, 130172071, 130197218, 130212435,
    130291076, 130402427, 130405166, 130676169, 130698471, 130713390,
    130785217, 131396908, 131455115, 131682095, 131689473, 131701956,
    132142800, 132525441, 132765492, 132801275, 133399034, 134522926,
    134524022, 134528994, 134532942, 134536993, 134538001, 134547533,
    134549216, 134549317, 134550159, 134550214, 134550232, 134551154,
    134551207, 134551390, 134553171, 134553237, 134554199, 134554227,
    134555229, 134555240, 134556118, 134557078, 134560099, 134560287,
    134562084, 134562180, 134563287, 134565083, 134566130, 134568111,
    134624737, 134666461, 134672998, 134684008, 134916523, 135100527,
    135219410, 135222083, 135306997, 135463054, 135779456, 135812968,
    136097030, 136131333, 136146907, 136157556, 136320962, 136450641,
    136466000, 136745792, 136761546, 136840245, 138160034, 138181244,
    138210071, 138210107, 138232200, 138237088, 138277547, 138287133,
    138306991, 138324625, 138341916, 138372035, 138372036, 138375118,
    138378071, 138380128, 138380194, 138534411, 138618294, 138931764,
    139536037, 139536038, 139536039, 139536040, 140367832, 140406050,
    140421668, 140422973, 140423713, 140436348, 140483470, 140615715,
    140802803, 140896470, 141189437, 141192744, 141382548, 141461680,
    141624996, 141627135, 141659139, 141872038, 141993721, 142006413,
    142045136, 142095667, 142227273, 142250480, 142518788, 142544931,
    142546314, 142555433, 142653044, 143191852, 143258496, 143299722,
    143331253, 143432462, 143632431, 143695326, 144219522, 144590916,
    145410436, 146720405, 150810438, 151258773, 151315554, 151400090,
    151429441, 151439617, 151453617, 151466296, 151511132, 151636561,
    151823192, 151827116, 151850642, 152016572, 153111675, 153114652,
    153123147, 153267108, 153389799, 153395366, 153718608, 154171028,
    154315227, 154559688, 154978771, 154979742, 154985773, 154989370,
    155044852, 155065458, 155578573, 156403304, 159085959, 159776047,
    159816630, 160733048, 160733049, 160733050, 160733051, 160733056,
    164824922, 164824924, 164824926, 164824930, 164831646, 164831647,
    164831648, 164831650, 164835050, 164835051, 164835052, 164835056,
    164835057, 164835059, 164836898, 164836899, 164836900, 164845333,
    164845334, 164845336, 164845337, 164845341, 164845342, 164845345,
    164845346, 164845349, 164845350, 164845353, 164845355, 164845358,
    164845361, 164845364, 164847387, 164847388, 164847389, 164847390,
    164848328, 164848329, 164848330, 164849448, 164849449, 164849451,
    164849452, 164849454, 164849455, 164849457, 164849461, 164850219,
    164850220, 164850222, 164850223, 164850224, 164850226, 164850227,
    164850228, 164850229, 164850231, 164850236, 164850237, 164850240,
    164850242, 164850243, 164852242, 164852243, 164852244, 164852247,
    164852248, 164852249, 164852250, 164852251, 164852254, 164852256,
    164852257, 164852258, 164852259, 164852260, 164852261, 164852262,
    164853061, 164853063, 164853071, 164853072, 164853075, 168118980,
    168118981, 168118983, 168118984, 168180375, 168180376, 168180378,
    168180379, 168270830, 168270831, 168270833, 168270834, 168270835,
]


@dataclass
class PackageInfo:
    id: str
    file_name: str
    digest: Optional[str] = None      # SHA1, used to match FileLocation in FE3 response
    sha256: Optional[str] = None      # SHA256 (AdditionalDigest), for integrity verification
    update_id: Optional[str] = None
    revision: Optional[str] = None
    url: Optional[str] = None
    architecture: Optional[str] = None


def _wu_soap_envelope(headers: dict, body: str) -> str:
    """Build a full SOAP envelope.

    headers dict keys:
      action       -- WU action name, appended to the ClientWebService base URL
      message_id   -- UUID string (without urn:uuid: prefix)
      to_url       -- WS-Addressing To URL
      created      -- WS-Security timestamp Created value
      expires      -- WS-Security timestamp Expires value
      ticket_token -- content of <TicketType>: "<User />" for GetCookie, release type otherwise
    """
    return f"""<s:Envelope xmlns:a="http://www.w3.org/2005/08/addressing" xmlns:s="http://www.w3.org/2003/05/soap-envelope">
    <s:Header>
        <a:Action s:mustUnderstand="1">http://www.microsoft.com/SoftwareDistribution/Server/ClientWebService/{headers["action"]}</a:Action>
        <a:MessageID>urn:uuid:{headers["message_id"]}</a:MessageID>
        <a:To s:mustUnderstand="1">{headers["to_url"]}</a:To>
        <o:Security s:mustUnderstand="1" xmlns:o="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd">
            <Timestamp xmlns="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-utility-1.0.xsd">
                <Created>{headers["created"]}</Created>
                <Expires>{headers["expires"]}</Expires>
            </Timestamp>
            <wuws:WindowsUpdateTicketsToken wsu:id="ClientMSA"
                xmlns:wsu="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-utility-1.0.xsd"
                xmlns:wuws="http://schemas.microsoft.com/msus/2014/10/WindowsUpdateAuthorization">
                <TicketType Name="MSA" Version="1.0" Policy="MBI_SSL">
                    {headers["ticket_token"]}
                </TicketType>
            </wuws:WindowsUpdateTicketsToken>
        </o:Security>
    </s:Header>
    <s:Body>
        <{headers["action"]} xmlns="http://www.microsoft.com/SoftwareDistribution/Server/ClientWebService">
            {body}
        </{headers["action"]}>
    </s:Body>
</s:Envelope>"""


def _soap_post(url: str, headers: dict, body: str) -> ET.Element:
    envelope = _wu_soap_envelope(headers, body)
    data = envelope.encode("utf-8")
    http_headers = {"Content-Type": "application/soap+xml; charset=utf-8"}
    req = urllib.request.Request(url, data=data, headers=http_headers, method="POST")
    with urllib.request.urlopen(req, context=_SSL_CTX) as resp:
        raw = resp.read()
    return ET.fromstring(raw.decode("utf-8"))


def _fmt(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def _fmt7(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond:07d}Z"


def _build_int_list(ids: List[int], tag: str) -> str:
    lines = [f"                    <int>{i}</int>" for i in ids]
    return f"                <{tag}>\n" + "\n".join(lines) + f"\n                </{tag}>"


def _device_attributes(locale: str, os_sku_id: int, os_version: str) -> str:
    return (
        "BranchReadinessLevel=CB;CurrentBranch=rs_prerelease;OEMModel=Virtual Machine;"
        "FlightRing=WIS;AttrDataVer=21;SystemManufacturer=Microsoft Corporation;"
        f"InstallLanguage={locale};OSUILocale={locale};InstallationType=Client;"
        "FlightingBranchName=external;FirmwareVersion=Hyper-V UEFI Release v2.5;"
        f"SystemProductName=Virtual Machine;OSSkuId={os_sku_id};FlightContent=Branch;"
        "App=WU;OEMName_Uncleaned=Microsoft Corporation;"
        f"AppVer={os_version};OSArchitecture=AMD64;SystemSKU=None;"
        "UpdateManagementGroup=2;IsFlightingEnabled=1;IsDeviceRetailDemo=0;"
        f"TelemetryLevel=3;OSVersion={os_version};DeviceFamily=Windows.Desktop;"
    )


def _find_ancestor_id(doc: ET.Element, target: ET.Element, depth: int) -> Optional[str]:
    """Find the <ID> text value that is `depth` levels above target in the tree."""
    parent_map = {child: parent for parent in doc.iter() for child in parent}
    current = target
    for _ in range(depth):
        current = parent_map.get(current)
        if current is None:
            return None
    for child in current:
        if child.tag.endswith("}ID") or child.tag == "ID":
            if child.text:
                return child.text
    return None

def _find_update_identity_for_secfrag(
    doc: ET.Element, sec_el: ET.Element
) -> Tuple[Optional[str], Optional[str]]:
    """Find UpdateID and RevisionNumber from the UpdateIdentity sibling of a SecuredFragment.

    Structure: SecuredFragment -> Properties -> Xml -> UpdateIdentity (attrs: UpdateID, RevisionNumber)
    Go 2 levels up from SecuredFragment to reach the Xml element, then get its first child.
    """
    parent_map = {child: parent for parent in doc.iter() for child in parent}
    current = sec_el
    for _ in range(2):
        current = parent_map.get(current)
        if current is None:
            return None, None
    # current is the Xml element; its first child should be UpdateIdentity
    first_child = list(current)
    if not first_child:
        return None, None
    identity_el = first_child[0]
    # UpdateID and RevisionNumber are attributes of UpdateIdentity, not child elements
    update_id = identity_el.get("UpdateID")
    revision = identity_el.get("RevisionNumber")
    return update_id, revision

def _bar_animation(percent):
    """Simple terminal progress bar animation [=====     ]"""
    bar_length = 30
    filled_length = int(bar_length * percent // 100)
    return f"[{'=' * filled_length}{' ' * (bar_length - filled_length)}] {percent}%"

def _download_progress_callback(block_num: int, block_size: int, total_size: int) -> None:
    """reporthook-style progress callback for file downloads."""
    downloaded = block_num * block_size
    if total_size > 0:
        percent = min(100, int(downloaded * 100 / total_size))
        downloaded_mb = downloaded / (1024 * 1024)
        total_mb = total_size / (1024 * 1024)
        print(f"\r   {_bar_animation(percent)} ({downloaded_mb:.1f}/{total_mb:.1f} MB)", end="", flush=True)
    else:
        print(f"\r   {downloaded / (1024 * 1024):.1f} MB downloaded...", end="", flush=True)


def _fetch_links_progress_callback(percent: int) -> None:
    """Custom progress bar hook for get_download_links"""
    if percent < 100:
        print(f"\r-> Fetching download link: {_bar_animation(percent)}", end="", flush=True)
    else:
        print(f"\r-> Fetching download link: {_bar_animation(100)}")


def get_download_links(
    product_id: str,
    architecture: Optional[str] = "auto",
    locale: str = "en-US",
    os_sku_id: int = 48,
    os_version: str = "10.0.16184.1001",
    main_only: bool = True,
    progress_callback: Optional[Callable[[int], None]] = None
) -> List[Dict]:
    release_type = "Retail"
    client_ws_url = "https://fe3.delivery.mp.microsoft.com/ClientWebService/client.asmx"
    fe3_url = "https://fe3.delivery.mp.microsoft.com/ClientWebService/client.asmx/secured"

    # --- Step 0: Fetch product data ---
    progress_callback and progress_callback(0)
    product_uri = (
        f"https://storeedgefd.dsx.mp.microsoft.com/v9.0/products/{product_id}"
        "?market=US&locale=en-us&deviceFamily=Windows.Desktop"
    )
    req = urllib.request.Request(product_uri)
    try:
        with urllib.request.urlopen(req, context=_SSL_CTX) as resp:
            prod_info = json.loads(resp.read().decode("utf-8"))
    except urllib.error.URLError as e:
        print(f"Error: Failed to fetch product data from Store Edge API: {e}", file=sys.stderr)
        sys.exit(1)

    if (prod_info.get("Payload", {}).get("DisplayPrice") or "").lower() not in ("free", "0.00", "0"):
        print(
            "Warning: The requested app is not a free app. "
            "The API may restrict downloading encrypted payload for paid apps.",
            file=sys.stderr,
        )

    skus = prod_info.get("Payload", {}).get("Skus", [])
    if not skus:
        print("Error: No SKUs found for this product.", file=sys.stderr)
        sys.exit(1)

    fulfillment_data_raw = skus[0].get("FulfillmentData")
    if not fulfillment_data_raw:
        print(
            "Warning: No fulfillment data found for this product. "
            "It may be an .exe installer rather than an Appx/MSIX package.",
            file=sys.stderr,
        )
        sys.exit(0)

    data_obj = json.loads(fulfillment_data_raw)
    wu_category_id = str(data_obj["WuCategoryId"])
    # Package name prefix used to identify the main package vs bundled dependencies
    package_family_name: str = data_obj.get("PackageFamilyName", "")
    main_package_prefix = package_family_name.split("_")[0] if package_family_name else ""

    # --- Step 1: Get Cookie ---
    progress_callback and progress_callback(25)
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    created = _fmt(now)
    cookie_offset = random.randint(-21600, 21600)
    last_change_offset = random.randint(-5184000, 5184000)
    expires_cookie = _fmt(now + timedelta(days=27, seconds=cookie_offset))
    expires_sync = _fmt(now + timedelta(minutes=5))
    last_change = _fmt7(now - timedelta(days=730) + timedelta(seconds=last_change_offset))
    current_time = _fmt(now + timedelta(milliseconds=7))

    cookie_resp = _soap_post(client_ws_url, {
        "action": "GetCookie",
        "message_id": "b9b43757-2247-4d7b-ae8f-a71ba8a22386",
        "to_url": client_ws_url,
        "created": created,
        "expires": expires_cookie,
        "ticket_token": "<User />",
    }, f"""
        <oldCookie></oldCookie>
        <lastChange>{last_change}</lastChange>
        <currentTime>{current_time}</currentTime>
        <protocolVersion>1.40</protocolVersion>
    """)
    # Find EncryptedData anywhere in the response tree
    encrypted_data_el = None
    for el in cookie_resp.iter():
        if el.tag.endswith("}EncryptedData") or el.tag == "EncryptedData":
            encrypted_data_el = el
            break
    if encrypted_data_el is None or not encrypted_data_el.text:
        print("Error: Could not retrieve cookie from ClientWebService.", file=sys.stderr)
        sys.exit(1)
    cookie_value = encrypted_data_el.text

    # --- Step 2: SyncUpdates ---
    progress_callback and progress_callback(50)
    non_leaf_xml = _build_int_list(INSTALLED_NON_LEAF_UPDATE_IDS, "InstalledNonLeafUpdateIDs")
    other_cached_xml = _build_int_list(OTHER_CACHED_UPDATE_IDS, "OtherCachedUpdateIDs")
    dev_attrs = _device_attributes(locale, os_sku_id, os_version)

    wu_resp = _soap_post(client_ws_url, {
        "action": "SyncUpdates",
        "message_id": "175df68c-4b91-41ee-b70b-f2208c65438e",
        "to_url": client_ws_url,
        "created": created,
        "expires": expires_sync,
        "ticket_token": release_type,
    }, f"""
            <cookie>
                <Expiration>2045-03-11T02:02:48Z</Expiration>
                <EncryptedData>{cookie_value}</EncryptedData>
            </cookie>
            <parameters>
                <ExpressQuery>false</ExpressQuery>
{non_leaf_xml}
{other_cached_xml}
                <SkipSoftwareSync>false</SkipSoftwareSync>
                <NeedTwoGroupOutOfScopeUpdates>true</NeedTwoGroupOutOfScopeUpdates>
                <FilterAppCategoryIds>
                    <CategoryIdentifier>
                        <Id>{wu_category_id}</Id>
                    </CategoryIdentifier>
                </FilterAppCategoryIds>
                <TreatAppCategoryIdsAsInstalled>true</TreatAppCategoryIdsAsInstalled>
                <AlsoPerformRegularSync>false</AlsoPerformRegularSync>
                <ComputerSpec/>
                <ExtendedUpdateInfoParameters>
                    <XmlUpdateFragmentTypes>
                        <XmlUpdateFragmentType>Extended</XmlUpdateFragmentType>
                    </XmlUpdateFragmentTypes>
                    <Locales>
                        <string>en-US</string>
                        <string>en</string>
                    </Locales>
                </ExtendedUpdateInfoParameters>
                <ClientPreferredLanguages>
                    <string>en-US</string>
                </ClientPreferredLanguages>
                <ProductsParameters>
                    <SyncCurrentVersionOnly>false</SyncCurrentVersionOnly>
                    <DeviceAttributes>{dev_attrs}</DeviceAttributes>
                    <CallerAttributes>Interactive=1;IsSeeker=0;</CallerAttributes>
                    <Products/>
                </ProductsParameters>
            </parameters>
        """)

    # The response may have escaped XML inside text nodes — unescape and re-parse
    raw_inner = ET.tostring(wu_resp, encoding="unicode")
    raw_inner = raw_inner.replace("&lt;", "<").replace("&gt;", ">")
    doc2 = ET.fromstring(raw_inner)

    # Collect package objects from <Files> elements.
    # Only take files that have an InstallerSpecificIdentifier (the actual package,
    # not DynamicMetadata .cab blockmap files).
    packages: List[PackageInfo] = []
    for files_el in doc2.iter():
        if not files_el.tag.endswith("}Files") and files_el.tag != "Files":
            continue
        for file_el in files_el:
            installer_id = file_el.get("InstallerSpecificIdentifier", "")
            if not installer_id:
                continue  # skip blockmap/metadata files (no InstallerSpecificIdentifier)
            file_name_attr = file_el.get("FileName", "")
            # Strip __PublisherHash blob: InstallerSpecificIdentifier is "Name_Version_Arch__Publisher"
            name_ver_arch = installer_id.split("__")[0]
            ext = pathlib.Path(file_name_attr).suffix or ".msix"
            file_name = f"{name_ver_arch}{ext}"
            digest = file_el.get("Digest")  # SHA1, for URL matching
            sha256 = next(
                (c.text for c in file_el if c.get("Algorithm") == "SHA256" and
                 (c.tag.endswith("}AdditionalDigest") or c.tag == "AdditionalDigest")),
                None,
            )

            pkg_id = _find_ancestor_id(doc2, files_el, depth=2)

            # Arch is the last underscore-separated segment of Name_Version_Arch
            arch_part = name_ver_arch.split("_")[-1]
            arch = arch_part if arch_part in ("x64", "x86", "arm64", "arm", "neutral") else None

            packages.append(PackageInfo(id=pkg_id or "", file_name=file_name, digest=digest, sha256=sha256, architecture=arch))

    # Collect UpdateID + Revision from <SecuredFragment> elements
    for sec_el in doc2.iter():
        if not (sec_el.tag.endswith("}SecuredFragment") or sec_el.tag == "SecuredFragment"):
            continue
        pkg_id = _find_ancestor_id(doc2, sec_el, depth=3)
        update_id, revision = _find_update_identity_for_secfrag(doc2, sec_el)
        for pkg in packages:
            if pkg.id == pkg_id:
                pkg.update_id = update_id
                pkg.revision = revision
                break

    packages = [p for p in packages if p.update_id is not None]

    if not packages:
        print("Error: No updates found for this product.", file=sys.stderr)
        sys.exit(1)

    # Pre-filter before making FE3 requests — avoids one round-trip per excluded package
    if architecture:
        packages = [p for p in packages if p.architecture in (architecture, "neutral")]

    if main_only and main_package_prefix:
        packages = [p for p in packages if p.file_name.startswith(main_package_prefix)]
        # Keep only the latest version per architecture
        def _version_tuple(p: PackageInfo) -> tuple:
            try:
                return tuple(int(x) for x in p.file_name.split("_")[1].split("."))
            except (IndexError, ValueError):
                return (0,)
        best: Dict[Optional[str], PackageInfo] = {}
        for p in packages:
            if p.architecture not in best or _version_tuple(p) > _version_tuple(best[p.architecture]):
                best[p.architecture] = p
        packages = list(best.values())

    # --- Step 3: GetExtendedUpdateInfo2 ---
    fe3_headers = {
        "action": "GetExtendedUpdateInfo2",
        "message_id": "2cc99c2e-3b3e-4fb1-9e31-0cd30e6f43a0",
        "to_url": fe3_url,
        "created": created,
        "expires": expires_sync,
        "ticket_token": release_type,
    }

    for pkg in packages:
        progress_callback and progress_callback(75 + (25 * packages.index(pkg) // len(packages)))
        fe3_resp = _soap_post(fe3_url, fe3_headers, f"""
            <updateIDs>
                <UpdateIdentity>
                    <UpdateID>{pkg.update_id}</UpdateID>
                    <RevisionNumber>{pkg.revision}</RevisionNumber>
                </UpdateIdentity>
            </updateIDs>
            <infoTypes>
                <XmlUpdateFragmentType>FileUrl</XmlUpdateFragmentType>
                <XmlUpdateFragmentType>FileDecryption</XmlUpdateFragmentType>
            </infoTypes>
            <deviceAttributes>{dev_attrs}</deviceAttributes>
        """)

        # Match the FileLocation by FileDigest so we get the real package URL,
        # not the blockmap/metadata .cab which may appear first in the response.
        for el in fe3_resp.iter():
            if el.tag.endswith("}FileLocation") or el.tag == "FileLocation":
                children = {
                    (c.tag.split("}")[1] if "}" in c.tag else c.tag): c.text
                    for c in el
                }
                file_digest = children.get("FileDigest")
                url = children.get("Url")
                if url and (pkg.digest is None or file_digest == pkg.digest):
                    pkg.url = url
                    break

    final = [
        {"FileName": p.file_name, "Architecture": p.architecture, "Url": p.url, "Digest": p.sha256}
        for p in packages
        if p.url
    ]
    progress_callback and progress_callback(100)
    return final


def _verify_sha256(filepath: pathlib.Path, digest_b64: str) -> bool:
    """Return True if the file's SHA256 matches the base64-encoded digest."""
    h = hashlib.sha256()
    with open(filepath, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return base64.b64encode(h.digest()).decode() == digest_b64


def download_packages(
    packages: List[Dict],
    download_dir: pathlib.Path,
    extract_dir: Optional[pathlib.Path] = None,
) -> None:
    """Download packages to *download_dir*, skipping files that already exist with a matching
    SHA256 digest.  If *extract_dir* is given, each package is also extracted into a
    subdirectory named after the package file stem (without extension).

    .msix / .appx / .msixbundle / .appxbundle files are ZIP archives and are extracted with
    the standard :mod:`zipfile` module.
    """
    download_dir.mkdir(parents=True, exist_ok=True)

    for pkg in packages:
        file_name = pkg["FileName"]
        url = pkg["Url"]
        digest = pkg.get("Digest")  # SHA256 base64, may be None
        dest = download_dir / file_name
        part = dest.with_suffix(dest.suffix + ".part")

        if dest.exists() and digest and _verify_sha256(dest, digest):
            print(f"  Skip  {file_name} (cached, digest OK)")
        else:
            if not digest:
                print(f"  Warning: no SHA256 digest available for {file_name} — cannot verify integrity", file=sys.stderr)

            print(f"  Downloading {file_name} ...")
            try:
                req = urllib.request.Request(url)
                with urllib.request.urlopen(req, context=_SSL_CTX) as resp:
                    total = int(resp.headers.get("Content-Length") or 0)
                    downloaded = 0
                    with open(part, "wb") as out:
                        block_num = 0
                        block_size = 65536
                        while True:
                            chunk = resp.read(block_size)
                            if not chunk:
                                break
                            out.write(chunk)
                            downloaded += len(chunk)
                            block_num += 1
                            _download_progress_callback(block_num, block_size, total)
                _download_progress_callback(max(1, downloaded // 65536), 65536, downloaded)
                print()
            except Exception as exc:
                part.unlink(missing_ok=True)
                print(f"  Error: download failed for {file_name}: {exc}", file=sys.stderr)
                continue

            if digest and not _verify_sha256(part, digest):
                part.unlink(missing_ok=True)
                print(f"  Error: digest mismatch for {file_name} — file deleted", file=sys.stderr)
                continue

            part.replace(dest)

        if extract_dir is not None:
            pkg_extract_dir = extract_dir / pathlib.Path(file_name).stem
            pkg_extract_dir.mkdir(parents=True, exist_ok=True)
            print(f"  Extracting {file_name} -> {pkg_extract_dir}")
            try:
                with zipfile.ZipFile(dest, "r") as zf:
                    zf.extractall(pkg_extract_dir)
                print(f"    Done ({len(list(pkg_extract_dir.iterdir()))} items)")
            except zipfile.BadZipFile as exc:
                print(f"  Error: could not extract {file_name}: {exc}", file=sys.stderr)



def main() -> None:
    parser = argparse.ArgumentParser(
        description="Retrieve direct download links for Microsoft Store apps."
    )
    parser.add_argument("product_id", help="The Product ID of the Microsoft Store app")
    parser.add_argument(
        "--architecture",
        choices=["x86", "x64", "arm", "arm64", "neutral", "auto"],
        help="Filter packages by architecture",
    )
    parser.add_argument(
        "--all-packages",
        action="store_true",
        help="Include bundled dependency packages, not just the main app package",
    )
    parser.add_argument("--locale", default="en-US", help="Locale for device attributes (default: en-US)")
    parser.add_argument("--os-sku-id", type=int, default=48, help="OS SKU ID (default: 48)")
    parser.add_argument("--os-version", default="10.0.16184.1001", help="Windows OS version string (default: 10.0.16184.1001)")
    parser.add_argument(
        "--download",
        nargs="?",
        const="",
        default=None,
        metavar="DIR",
        help="Download packages. Optionally specify download directory (default: ~/Downloads)",
    )
    parser.add_argument(
        "--extract",
        nargs="?",
        const="",
        default=None,
        metavar="DIR",
        help="Extract packages after downloading. Optionally specify extract directory (default: download directory)",
    )
    args = parser.parse_args()

    # Resolve --download / --extract paths
    download_dir: Optional[pathlib.Path] = None
    if args.download is not None:
        raw = args.download
        download_dir = (pathlib.Path(raw) if raw else pathlib.Path.home() / "Downloads").expanduser().resolve()

    extract_dir: Optional[pathlib.Path] = None
    if args.extract is not None:
        if download_dir is None:
            print("Error: --extract requires --download", file=sys.stderr)
            sys.exit(1)
        raw = args.extract
        extract_dir = (pathlib.Path(raw) if raw else download_dir).expanduser().resolve()

    results = get_download_links(
        product_id=args.product_id,
        architecture=_current_arch() if args.architecture == "auto" else args.architecture,
        locale=args.locale,
        os_sku_id=args.os_sku_id,
        os_version=args.os_version,
        main_only=not args.all_packages,
        progress_callback=_fetch_links_progress_callback
    )

    if not results:
        print("No download links found.", file=sys.stderr)
        sys.exit(1)

    for pkg in results:
        print(f"FileName:     {pkg['FileName']}")
        print(f"Architecture: {pkg['Architecture']}")
        print(f"Url:          {pkg['Url']}")
        if pkg.get("Digest"):
            print(f"Digest:       {pkg['Digest']} (SHA256)")
        print()

    if download_dir is not None:
        print(f"Downloading to {download_dir} ...")
        download_packages(results, download_dir, extract_dir)


if __name__ == "__main__":
    main()
