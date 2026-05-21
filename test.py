import streamlit as st
import pandas as pd
import gspread
from google.oauth2 import service_account
from PIL import Image, UnidentifiedImageError, ImageOps
import io
import datetime
import re
import base64
import math
import cloudinary
import cloudinary.uploader
from pillow_heif import register_heif_opener
from pyzbar.pyzbar import decode
from streamlit_geolocation import streamlit_geolocation

# Enable HEIC support in PIL
register_heif_opener()

# ==========================================
# 1. CONFIGURATION & CONSTANTS
# ==========================================
st.set_page_config(page_title="SPO Lot Defect System", page_icon="🏭", layout="centered")

DATE_FORMAT = "%Y-%m-%d"
MAX_FILE_SIZE_MB = 10
MAX_FILE_SIZE_BYTES = MAX_FILE_SIZE_MB * 1024 * 1024

# Geofence Constants
FACTORY_LAT = 4.574355483324381
FACTORY_LON = 101.10586181352994
ALLOWED_RADIUS_METERS = 100

# Configure Cloudinary credentials from secrets
cloudinary.config(
    cloud_name = st.secrets["CLOUDINARY_CLOUD_NAME"],
    api_key = st.secrets["CLOUDINARY_API_KEY"],
    api_secret = st.secrets["CLOUDINARY_API_SECRET"],
    secure = True
)

# ==========================================
# 2. GEOFENCE SECURITY GATEKEEPER
# ==========================================
def get_distance_meters(lat1, lon1, lat2, lon2):
    """Calculates distance between two GPS points using the Haversine formula."""
    R = 6371000 # Radius of Earth in meters
    phi_1 = math.radians(lat1)
    phi_2 = math.radians(lat2)
    delta_phi = math.radians(lat2 - lat1)
    delta_lambda = math.radians(lon2 - lon1)

    a = math.sin(delta_phi / 2.0) ** 2 + \
        math.cos(phi_1) * math.cos(phi_2) * \
        math.sin(delta_lambda / 2.0) ** 2
    
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return R * c

st.title("📍 Location Verification Required")
st.write("You must be on the factory floor to access this system.")

# Request user location via browser
location = streamlit_geolocation()

if location and location.get('latitude') is not None and location.get('longitude') is not None:
    user_lat = location['latitude']
    user_lon = location['longitude']
    
    distance = get_distance_meters(FACTORY_LAT, FACTORY_LON, user_lat, user_lon)
    
    if distance <= ALLOWED_RADIUS_METERS:
        st.success(f"Location verified! Distance: {int(distance)}m.")
        st.divider()
    else:
        st.error(f"Access Denied. You are {int(distance)} meters away. You must be within {ALLOWED_RADIUS_METERS}m of the factory.")
        st.stop() # Immediately halts execution of the rest of the script
else:
    st.warning("Please click the button above and allow location access in your browser to continue.")
    st.stop() # Halts execution until location is provided


# ==========================================
# 3. STATE INITIALIZATION
# ==========================================
if "defect_categories" not in st.session_state:
    st.session_state.defect_categories = ["Bend Lead", "Scratches", "Expose Copper", "Contam", "Flashes", "Delam"]

# ==========================================
# 4. GOOGLE SHEETS CLIENT
# ==========================================
@st.cache_resource
def get_gcp_credentials():
    try:
        creds_dict = dict(st.secrets["gcp_service_account"])
        creds_dict["private_key"] = creds_dict["private_key"].replace("\\n", "\n")
        return service_account.Credentials.from_service_account_info(
            creds_dict,
            scopes=["https://www.googleapis.com/auth/spreadsheets"]
        )
    except Exception as e:
        st.error(f"Missing or invalid Google Cloud credentials in secrets.toml. Error: {e}")
        st.stop()

def get_sheets_client():
    return gspread.authorize(get_gcp_credentials())

# ==========================================
# 5. CLOUDINARY & MAX SECURITY IMAGE PROCESSING
# ==========================================
def sanitize_and_process_image(image_file, spo, lot_id, defect_type):
    """
    Max Security Check: Validates size, structure, and strips out all metadata/hidden payloads 
    by rebuilding the image pixel by pixel before processing.
    """
    image_bytes = image_file.getvalue()
    
    # 1. Hard Size Limit
    if len(image_bytes) > MAX_FILE_SIZE_BYTES:
        raise ValueError(f"File exceeds the {MAX_FILE_SIZE_MB}MB strict limit.")

    try:
        # 2. Structural Verification
        img = Image.open(io.BytesIO(image_bytes))
        img.verify() # Ensures it's a real image file, not a renamed executable
        
        # 3. Payload Destruction (Re-encoding)
        # We reopen it because verify() closes the file pointer
        img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
        
        # Create a blank canvas and paste only the visual pixels (drops EXIF/malware)
        clean_img = Image.new("RGB", img.size)
        clean_img.putdata(list(img.getdata()))
        
        # Save the sanitized image to memory
        output = io.BytesIO()
        clean_img.save(output, format="JPEG", quality=85)
        sanitized_bytes = output.getvalue()
        
    except (UnidentifiedImageError, Exception) as e:
        raise ValueError("Corrupted, disguised, or malicious file detected. Upload blocked.")

    # Format the filename
    clean_spo = "".join(c for c in str(spo) if c.isalnum())
    clean_lot = "".join(c for c in str(lot_id) if c.isalnum() or c in ('-', '_'))
    clean_cat = "".join(c for c in str(defect_type) if c.isalnum())
    timestamp = datetime.datetime.now().strftime("%H%M%S")
    filename = f"{datetime.date.today().strftime(DATE_FORMAT)}_{clean_spo}_{clean_lot}_{clean_cat}_{timestamp}.jpg"
    
    return sanitized_bytes, filename

def upload_image_to_cloudinary(image_bytes, filename):
    try:
        base64_image = "data:image/jpeg;base64," + base64.b64encode(image_bytes).decode('utf-8')
        response = cloudinary.uploader.upload(
            base64_image,
            public_id=filename.split('.')[0], 
            folder="SPO_Defects"
        )
        return response.get("secure_url")
    except Exception as e:
        st.error(f"Failed to upload image to Cloudinary: {e}")
        return None

# ==========================================
# 6. GOOGLE SHEETS (DATABASE)
# ==========================================
def open_spreadsheet():
    client = get_sheets_client()
    return client.open_by_key(st.secrets["GOOGLE_SHEET_ID"])

def get_expected_headers():
    return ["Date", "SPO", "Lot ID"] + st.session_state.defect_categories + ["Remark", "Created At", "Updated At"]

def ensure_headers(worksheet):
    headers = get_expected_headers()
    if not worksheet.row_values(1):
        worksheet.insert_row(headers, 1)
        worksheet.format("A1:Z1", {"textFormat": {"bold": True}})
        worksheet.freeze(rows=1)

def get_or_create_date_sheet(date_str):
    doc = open_spreadsheet()
    try:
        ws = doc.worksheet(date_str)
    except gspread.exceptions.WorksheetNotFound:
        ws = doc.add_worksheet(title=date_str, rows="1000", cols="26")
    ensure_headers(ws)
    return ws

def append_or_update_record(date_str, spo, lot_id, defect_links, remark):
    ws = get_or_create_date_sheet(date_str)
    records = ws.get_all_records()
    df = pd.DataFrame(records)
    
    now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    row_map = {"Date": date_str, "SPO": str(spo), "Lot ID": str(lot_id), "Remark": remark, "Updated At": now_str}
    row_index = -1
    
    if not df.empty and "SPO" in df.columns and "Lot ID" in df.columns:
        match = df[(df["SPO"].astype(str) == str(spo)) & (df["Lot ID"].astype(str) == str(lot_id))]
        if not match.empty:
            row_index = int(match.index[0]) + 2
            row_map["Created At"] = df.iloc[match.index[0]].get("Created At", now_str)
            for cat in st.session_state.defect_categories:
                row_map[cat] = df.iloc[match.index[0]].get(cat, "")
    
    if "Created At" not in row_map:
        row_map["Created At"] = now_str
        for cat in st.session_state.defect_categories:
            row_map.setdefault(cat, "")

    for cat, link in defect_links.items():
        if cat in row_map and row_map[cat]:
            row_map[cat] = f"{row_map[cat]}, {link}"
        else:
            row_map[cat] = link

    headers = get_expected_headers()
    ordered_row = [row_map.get(h, "") for h in headers]
    
    if row_index != -1:
        ws.update(range_name=f"A{row_index}", values=[ordered_row], value_input_option="USER_ENTERED")
    else:
        ws.append_row(ordered_row, value_input_option="USER_ENTERED")
    return True

# ==========================================
# 7. UI ROUTING & INTERFACE
# ==========================================

# Sidebar Navigation (Stripped Down)
st.sidebar.markdown("### 🏭 SPO System Menu")
nav_options = ["Add Inspection", "Settings"]
choice = st.sidebar.radio("Navigation", nav_options)

# --- ADD INSPECTION ---
if choice == "Add Inspection":
    st.title("📥 Add Inspection")
    
    with st.form("add_form"):
        date_val = st.date_input("Date", datetime.date.today())
        spo_val = st.text_input("SPO").strip()
        
        st.markdown("---")
        st.markdown("### 📸 Lot ID Scanner")
        scan_mode = st.radio("Lot ID Input Method", ["Scan Barcode", "Manual Entry"])
        
        lot_val = ""
        if scan_mode == "Scan Barcode":
            barcode_pic = st.camera_input("Take a picture of the Lot ID Barcode")
            if barcode_pic:
                img = Image.open(barcode_pic)
                # Enhancement: Convert image to grayscale to boost scanner accuracy
                gray_img = ImageOps.grayscale(img)
                decoded = decode(gray_img)
                
                if decoded:
                    lot_val = decoded[0].data.decode("utf-8")
                    st.success(f"✅ Scanned Successfully: {lot_val}")
                else:
                    st.error("❌ No barcode detected. Ensure it is clear and well-lit.")
        
        lot_val_input = st.text_input("Lot ID", value=lot_val).strip()
        
        st.markdown("---")
        st.markdown("### 🔍 Defects")
        selected_defects = st.multiselect("Select Existing Defects", st.session_state.defect_categories)
        new_custom_defect = st.text_input("Or Add New Defect (Type name here)")
        
        final_defects_list = list(selected_defects)
        if new_custom_defect and new_custom_defect not in final_defects_list:
            final_defects_list.append(new_custom_defect.strip())
            
        upload_mode = st.radio("Defect Image Input Method", ["Camera", "Upload File"])
        images_map = {}
        
        if final_defects_list:
            for cat in final_defects_list:
                if upload_mode == "Upload File":
                    # Strictly lock file types to JPG, JPEG, and HEIC
                    img = st.file_uploader(f"Image for {cat} (Max 10MB)", type=["jpg", "jpeg", "heic"], key=f"up_{cat}")
                else:
                    img = st.camera_input(f"Capture {cat}", key=f"cam_{cat}")
                if img: images_map[cat] = img
        
        remark_val = st.text_area("Remark")
        
        if st.form_submit_button("Submit Record", use_container_width=True):
            if not spo_val or not lot_val_input or not final_defects_list:
                st.error("SPO, Lot ID, and at least one Defect Category are required.")
            elif len(images_map) != len(final_defects_list):
                st.error("An image must be provided for EVERY selected defect.")
            else:
                with st.spinner("Sanitizing images & saving to secure database..."):
                    if new_custom_defect and new_custom_defect not in st.session_state.defect_categories:
                        st.session_state.defect_categories.append(new_custom_defect.strip())

                    uploaded_links = {}
                    error_flag = False
                    
                    for cat, img_file in images_map.items():
                        try:
                            # Run the Max Security check
                            img_bytes, filename = sanitize_and_process_image(img_file, spo_val, lot_val_input, cat)
                            link = upload_image_to_cloudinary(img_bytes, filename)
                            
                            if link:
                                uploaded_links[cat] = f'=IMAGE("{link}")'
                            else:
                                error_flag = True
                                break
                        except ValueError as ve:
                            st.error(f"Security Alert for {cat}: {ve}")
                            error_flag = True
                            break
                            
                    if not error_flag:
                        success = append_or_update_record(date_val.strftime(DATE_FORMAT), spo_val, lot_val_input, uploaded_links, remark_val)
                        if success:
                            st.success("Record saved successfully!")
                            st.balloons()
                        else:
                            st.error("Failed to save to Google Sheets.")

# --- SETTINGS ---
elif choice == "Settings":
    st.title("⚙️ Settings")
    st.markdown("Modify the global list of inspection defect types.")
    
    st.subheader("Add Defect Category")
    new_defect = st.text_input("New Defect Name")
    if st.button("Add Defect", use_container_width=True):
        if new_defect and new_defect not in st.session_state.defect_categories:
            st.session_state.defect_categories.append(new_defect.strip())
            st.success(f"Added '{new_defect}'!")
            st.rerun()
        elif new_defect in st.session_state.defect_categories:
            st.error("Category already exists.")
            
    st.markdown("---")
    st.subheader("Remove Defect Category")
    remove_defect = st.selectbox("Select Defect to Remove", st.session_state.defect_categories)
    if st.button("Remove Defect", use_container_width=True):
        if len(st.session_state.defect_categories) > 1:
            st.session_state.defect_categories.remove(remove_defect)
            st.success(f"Removed '{remove_defect}'.")
            st.rerun()
        else:
            st.error("You must have at least one defect category.")
