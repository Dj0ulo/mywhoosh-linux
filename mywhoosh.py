import os
import sys
import tempfile
import urllib.request
import zipfile

from get_msstore_download_links import get_download_links


def download_and_extract_msix(destination_dir):
    """
    Downloads a .msix file safely into RAM (/tmp) and extracts 
    its contents directly into the permanent Wine prefix directory.
    """
    # Ensure the target directory inside your Wine prefix exists
    os.makedirs(destination_dir, exist_ok=True)

    def bar_animation(percent):
        # Simple terminal progress bar animation [=====     ]
        bar_length = 30
        filled_length = int(bar_length * percent // 100)
        return f"[{'=' * filled_length}{' ' * (bar_length - filled_length)}] {percent}%"

    # Custom progress bar hook for get_download_links
    def fetch_links_progress_callback(percent):
        if percent < 100:       
            sys.stdout.write(f"\r-> Fetching download link: {bar_animation(percent)}")
            sys.stdout.flush()
        else:
            sys.stdout.write(f"\r-> Fetching download link: {bar_animation(100)}\n")
            sys.stdout.flush()

    
    # Custom progress bar hook for urllib
    def download_progress_callback(block_num, block_size, total_size):
        downloaded = block_num * block_size
        if total_size > 0:
            percent = min(100, int(downloaded * 100 / total_size))
            # Convert bytes to Megabytes for cleaner readability
            downloaded_mb = downloaded / (1024 * 1024)
            total_mb = total_size / (1024 * 1024)

            sys.stdout.write(f"\r-> Downloading Installer: {bar_animation(percent)} ({downloaded_mb:.1f}/{total_mb:.1f} MB)")
            sys.stdout.flush()
        else:
            sys.stdout.write(f"\r-> Downloading Installer: {downloaded / (1024 * 1024):.1f} MB downloaded...")
            sys.stdout.flush()

    # 1. Create a secure, self-cleaning temporary directory
    print("Initializing installer setup...")
    with tempfile.TemporaryDirectory() as tmp_dir:
        results = get_download_links('9ndh0f2vhzx2', 'x64', progress_callback=fetch_links_progress_callback)
        pkg = next((r for r in results if 'mywhoosh' in r.get('FileName', '').lower()), None)
        url = pkg['Url']

        msix_temp_path = os.path.join(tmp_dir, pkg['FileName'])
        
        # 2. Download directly to /tmp (usually RAM on Linux)
        try:
            urllib.request.urlretrieve(url, msix_temp_path, download_progress_callback)
            print("\n✓ Download finished successfully!")
        except Exception as e:
            print(f"\n❌ Error downloading file: {e}")
            return False
            
        # 3. Extract directly to permanent home inside Wine prefix
        print(f"-> Extracting files to: {destination_dir}")
        try:
            with zipfile.ZipFile(msix_temp_path, 'r') as zip_ref:
                # Get total number of files to show progress if you want, 
                # but standard extractall is fastest.
                zip_ref.extractall(destination_dir)
            print("✓ Extraction complete! Game files ready.")
            return True
        except zipfile.BadZipFile:
            print("❌ Error: The downloaded MSIX file was corrupted or incomplete.")
            return False
        except Exception as e:
            print(f"❌ Error during extraction: {e}")
            return False
        
if __name__ == "__main__":
    # Replace this with the actual MyWhoosh Direct Download URL
    MSIX_URL = "https://example.com/placeholder-mywhoosh.msix" 
    
    # Target location inside your user's Wine prefix
    MYWHOOSH_DIR = os.path.expanduser("~/Games/MyWhoosh")
    
    success = download_and_extract_msix(MYWHOOSH_DIR)
    if success:
        print("\nSetup finished perfectly. You can now launch MyWhoosh via Wine!")