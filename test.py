import streamlit as st
import pandas as pd
import gspread
from google.oauth2 import service_account
from PIL import Image
import io
import datetime
import hashlib
import re
import plotly.express as px
import base64
import cloudinary
import cloudinary.uploader

# ==========================================
# 1. CONFIGURATION & CONSTANTS
# ==========================================
st.set_page_config(page_title="SPO Lot Defect System", page_icon="🏭", layout="wide")

DEFECT_CATEGORIES = ["Bend Lead", "Scratches", "Expose Copper", "Contam", "Flashes", "Delam"]
DATE_FORMAT = "%Y-%m-%d"

# Configure Cloudinary credentials from secrets
cloudinary.config(
    cloud_name = st.secrets["CLOUDINARY_CLOUD_NAME"],
    api_key = st.secrets["CLOUDINARY_API_KEY"],
    api_secret = st.secrets["CLOUDINARY_API_SECRET"],
    secure = True
)

# ==========================================
# 2. AUTHENTICATION LOGIC
# ==========================================
def hash_password(password: str) -> str:
    return hashlib.sha256(password.encode()).hexdigest()

USER_CREDENTIALS = {
    "admin": {"password": hash_password("admin123"), "role": "Admin", "name": "System Admin"},
    "inspector": {"password": hash_password("inspect123"), "role": "Inspector", "name": "Line Inspector"}
}

def init_auth():
    if "authenticated" not in st.session_state:
        st.session_state.authenticated = False
        st.session_state.username = None
        st.session_state.role = None
        st.session_state.user_fullname = None

def login(username, password):
    if username in USER_CREDENTIALS and USER_CREDENTIALS[username]["password"] == hash_password(password):
        st.session_state.authenticated = True
        st.session_state.username = username
        st.session_state.role = USER_CREDENTIALS[username]["role"]
        st.session_state.user_fullname = USER_CREDENTIALS[username]["name"]
        return True
    return False

def logout():
    st.session_state.authenticated = False
    st.session_state.username = None
    st.session_state.role = None
    st.rerun()

# ==========================================
# 3. GOOGLE SHEETS CLIENT
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
# 4. CLOUDINARY (IMAGE STORAGE)
# ==========================================
def upload_image_to_cloudinary(image_bytes, filename):
    try:
        # Convert bytes to base64 so Cloudinary can process it securely
        base64_image = "data:image/jpeg;base64," + base64.b64encode(image_bytes).decode('utf-8')
        
        # Upload to Cloudinary into a specific folder
        response = cloudinary.uploader.upload(
            base64_image,
            public_id=filename.split('.')[0], # Remove .jpg for the public ID
            folder="SPO_Defects"
        )
        # Return the secure HTTPS URL
        return response.get("secure_url")
    except Exception as e:
        st.error(f"Failed to upload image to Cloudinary: {e}")
        return None

def process_image(image_file, spo, lot_id, defect_type):
    img = Image.open(image_file).convert("RGB")
    output = io.BytesIO()
    img.save(output, format="JPEG", quality=85)
    
    clean_spo = "".join(c for c in str(spo) if c.isalnum())
    clean_lot = "".join(c for c in str(lot_id) if c.isalnum() or c in ('-', '_'))
    clean_cat = "".join(c for c in str(defect_type) if c.isalnum())
    timestamp = datetime.datetime.now().strftime("%H%M%S")
    
    filename = f"{datetime.date.today().strftime(DATE_FORMAT)}_{clean_spo}_{clean_lot}_{clean_cat}_{timestamp}.jpg"
    return output.getvalue(), filename

# ==========================================
# 5. GOOGLE SHEETS (DATABASE)
# ==========================================
def open_spreadsheet():
    client = get_sheets_client()
    return client.open_by_key(st.secrets["GOOGLE_SHEET_ID"])

def get_expected_headers():
    return ["Date", "SPO", "Lot ID"] + DEFECT_CATEGORIES + ["Remark", "Created At", "Updated At"]

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
            for cat in DEFECT_CATEGORIES:
                row_map[cat] = df.iloc[match.index[0]].get(cat, "")
    
    if "Created At" not in row_map:
        row_map["Created At"] = now_str
        for cat in DEFECT_CATEGORIES:
            row_map.setdefault(cat, "")

    for cat, link in defect_links.items():
        if cat in row_map and row_map[cat]:
            row_map[cat] = f"{row_map[cat]}, {link}"
        else:
            row_map[cat] = link

    headers = get_expected_headers()
    ordered_row = [row_map.get(h, "") for h in headers]
    
    if row_index != -1:
        ws.update(range_name=f"A{row_index}", values=[ordered_row])
    else:
        ws.append_row(ordered_row)
    return True

def get_records_by_date(date_str):
    doc = open_spreadsheet()
    try:
        return pd.DataFrame(doc.worksheet(date_str).get_all_records())
    except gspread.exceptions.WorksheetNotFound:
        return pd.DataFrame()

def get_all_records():
    doc = open_spreadsheet()
    all_dfs = []
    for ws in doc.worksheets():
        if re.match(r'^\d{4}-\d{2}-\d{2}$', ws.title):
            data = ws.get_all_records()
            if data:
                all_dfs.append(pd.DataFrame(data))
    return pd.concat(all_dfs, ignore_index=True) if all_dfs else pd.DataFrame()

def delete_record(date_str, spo, lot_id):
    doc = open_spreadsheet()
    try:
        ws = doc.worksheet(date_str)
        df = pd.DataFrame(ws.get_all_records())
        match = df[(df["SPO"].astype(str) == str(spo)) & (df["Lot ID"].astype(str) == str(lot_id))]
        if not match.empty:
            ws.delete_rows(int(match.index[0]) + 2)
            return True
    except Exception:
        pass
    return False

def update_record(old_date, new_date, spo, lot_id, updated_data):
    if old_date != new_date:
        delete_record(old_date, spo, lot_id)
        ws_new = get_or_create_date_sheet(new_date)
        ws_new.append_row([updated_data.get(h, "") for h in get_expected_headers()])
    else:
        doc = open_spreadsheet()
        ws = doc.worksheet(old_date)
        df = pd.DataFrame(ws.get_all_records())
        match = df[(df["SPO"].astype(str) == str(spo)) & (df["Lot ID"].astype(str) == str(lot_id))]
        if not match.empty:
            ws.update(range_name=f"A{int(match.index[0]) + 2}", values=[[updated_data.get(h, "") for h in get_expected_headers()]])
    return True

# ==========================================
# 6. UI ROUTING & INTERFACE
# ==========================================
init_auth()

if not st.session_state.authenticated:
    st.markdown("<h2 style='text-align: center;'>🏭 SPO Lot Defect System</h2>", unsafe_allow_html=True)
    with st.form("login_form"):
        user_input = st.text_input("Username")
        pwd_input = st.text_input("Password", type="password")
        if st.form_submit_button("Login", use_container_width=True):
            if login(user_input, pwd_input):
                st.rerun()
            else:
                st.error("Invalid credentials.")
    st.stop()

# Sidebar Navigation
st.sidebar.markdown(f"**User:** {st.session_state.user_fullname} ({st.session_state.role})")
nav_options = ["Dashboard", "Add Inspection", "View Records", "Daily Inspection View"]
if st.session_state.role == "Admin":
    nav_options += ["Edit Records", "Export Data", "Settings"]

choice = st.sidebar.radio("Navigation", nav_options)
if st.sidebar.button("Logout"):
    logout()

# Helper for displaying image links
def format_links(val):
    if not val: return ""
    links = [l.strip() for l in str(val).split(",") if l.strip()]
    return " | ".join([f'<a href="{l}" target="_blank">🔗 Image {i+1}</a>' for i, l in enumerate(links)])

# --- DASHBOARD ---
if choice == "Dashboard":
    st.title("📊 Dashboard")
    with st.spinner("Loading records..."):
        df = get_all_records()
    
    if df.empty:
        st.info("No data available.")
    else:
        total_lots = len(df)
        defect_counts = {c: df[c].apply(lambda x: len(str(x).split(",")) if pd.notna(x) and str(x).strip() else 0).sum() for c in DEFECT_CATEGORIES if c in df.columns}
        
        c1, c2, c3 = st.columns(3)
        c1.metric("Total Lots Inspected", total_lots)
        c2.metric("Total Defects", sum(defect_counts.values()))
        c3.metric("Defects per Lot", round(sum(defect_counts.values()) / total_lots, 2))
        
        st.markdown("---")
        col1, col2 = st.columns(2)
        df_chart = pd.DataFrame({"Category": defect_counts.keys(), "Count": defect_counts.values()})
        with col1:
            st.plotly_chart(px.bar(df_chart, x="Category", y="Count", title="Defects by Category"), use_container_width=True)
        with col2:
            st.plotly_chart(px.pie(df_chart, names="Category", values="Count", title="Defect Distribution", hole=0.4), use_container_width=True)

# --- ADD INSPECTION ---
elif choice == "Add Inspection":
    st.title("📥 Add Inspection")
    with st.form("add_form"):
        c1, c2, c3 = st.columns(3)
        date_val = c1.date_input("Date", datetime.date.today())
        spo_val = c2.text_input("SPO").strip()
        lot_val = c3.text_input("Lot ID").strip()
        
        selected_defects = st.multiselect("Defect Categories", DEFECT_CATEGORIES)
        
        upload_mode = st.radio("Image Input Method", ["Upload File", "Camera"])
        images_map = {}
        
        if selected_defects:
            for cat in selected_defects:
                if upload_mode == "Upload File":
                    img = st.file_uploader(f"Image for {cat}", type=["jpg", "png", "jpeg"], key=f"up_{cat}")
                else:
                    img = st.camera_input(f"Capture {cat}", key=f"cam_{cat}")
                if img: images_map[cat] = img
        
        remark_val = st.text_area("Remark")
        
        if st.form_submit_button("Submit Record", use_container_width=True):
            if not spo_val or not lot_val or not selected_defects:
                st.error("SPO, Lot ID, and at least one Defect Category are required.")
            elif len(images_map) != len(selected_defects):
                st.error("An image must be provided for EVERY selected defect category.")
            else:
                with st.spinner("Uploading images to Cloudinary & saving to database..."):
                    uploaded_links = {}
                    error_flag = False
                    
                    for cat, img_file in images_map.items():
                        img_bytes, filename = process_image(img_file, spo_val, lot_val, cat)
                        link = upload_image_to_cloudinary(img_bytes, filename)
                        if link:
                            uploaded_links[cat] = link
                        else:
                            error_flag = True
                            break
                            
                    if not error_flag:
                        success = append_or_update_record(date_val.strftime(DATE_FORMAT), spo_val, lot_val, uploaded_links, remark_val)
                        if success:
                            st.success("Record saved successfully!")
                            st.balloons()
                        else:
                            st.error("Failed to save to Google Sheets.")

# --- VIEW RECORDS ---
elif choice == "View Records":
    st.title("🔍 View Records")
    with st.expander("Filters", expanded=True):
        c1, c2, c3 = st.columns(3)
        f_date = c1.date_input("Filter by Date", value=None)
        f_spo = c2.text_input("Filter by SPO").strip()
        f_lot = c3.text_input("Filter by Lot ID").strip()

    df = get_records_by_date(f_date.strftime(DATE_FORMAT)) if f_date else get_all_records()
    
    if not df.empty:
        if f_spo: df = df[df["SPO"].astype(str).str.contains(f_spo, case=False)]
        if f_lot: df = df[df["Lot ID"].astype(str).str.contains(f_lot, case=False)]
        
        if not df.empty:
            df_display = df.copy()
            for cat in DEFECT_CATEGORIES:
                if cat in df_display.columns:
                    df_display[cat] = df_display[cat].apply(format_links)
            st.markdown(df_display.to_html(escape=False, index=False), unsafe_allow_html=True)
            
            st.subheader("Image Previews")
            for _, row in df.iterrows():
                with st.expander(f"{row.get('Date')} | SPO: {row.get('SPO')} | Lot: {row.get('Lot ID')}"):
                    for cat in DEFECT_CATEGORIES:
                        if cat in row and str(row[cat]).strip():
                            st.markdown(f"**{cat}**")
                            for link in str(row[cat]).split(","):
                                st.image(link.strip(), width=300)
        else:
            st.warning("No records match your filters.")
    else:
        st.info("No records found.")

# --- DAILY INSPECTION VIEW ---
elif choice == "Daily Inspection View":
    st.title("📅 Daily View")
    target_date = st.date_input("Select Date", datetime.date.today())
    df = get_records_by_date(target_date.strftime(DATE_FORMAT))
    
    if not df.empty:
        total_lots = len(df)
        st.subheader(f"Summary for {target_date.strftime(DATE_FORMAT)}")
        m_cols = st.columns(len(DEFECT_CATEGORIES) + 1)
        m_cols[0].metric("Total Lots", total_lots)
        for i, cat in enumerate(DEFECT_CATEGORIES):
            count = df[cat].apply(lambda x: len(str(x).split(",")) if pd.notna(x) and str(x).strip() else 0).sum() if cat in df.columns else 0
            m_cols[i+1].metric(cat, count)
        
        df_disp = df.copy()
        for cat in DEFECT_CATEGORIES:
            if cat in df_disp.columns:
                df_disp[cat] = df_disp[cat].apply(format_links)
        st.markdown(df_disp.to_html(escape=False, index=False), unsafe_allow_html=True)
    else:
        st.info("No records for this date.")

# --- EDIT RECORDS ---
elif choice == "Edit Records":
    st.title("🛠️ Edit Records")
    c1, c2, c3 = st.columns(3)
    s_date = c1.date_input("Record Date", datetime.date.today())
    s_spo = c2.text_input("Record SPO").strip()
    s_lot = c3.text_input("Record Lot ID").strip()
    
    if st.button("Search Record"):
        df = get_records_by_date(s_date.strftime(DATE_FORMAT))
        if not df.empty:
            match = df[(df["SPO"].astype(str) == str(s_spo)) & (df["Lot ID"].astype(str) == str(s_lot))]
            if not match.empty:
                st.session_state.edit_target = match.iloc[0].to_dict()
                st.success("Record found.")
            else:
                st.session_state.edit_target = None
                st.error("Record not found.")
                
    if "edit_target" in st.session_state and st.session_state.edit_target:
        row = st.session_state.edit_target
        with st.form("edit_form"):
            new_date = st.date_input("Date", datetime.datetime.strptime(row["Date"], DATE_FORMAT).date())
            new_spo = st.text_input("SPO", row["SPO"])
            new_lot = st.text_input("Lot ID", row["Lot ID"])
            new_rem = st.text_area("Remark", row.get("Remark", ""))
            
            links = {cat: st.text_input(f"{cat} Image Link", row.get(cat, "")) for cat in DEFECT_CATEGORIES}
            
            action = st.radio("Action", ["Update", "Delete Record"])
            if st.form_submit_button("Execute"):
                old_date = s_date.strftime(DATE_FORMAT)
                if action == "Delete Record":
                    if delete_record(old_date, row["SPO"], row["Lot ID"]):
                        st.success("Deleted successfully.")
                        st.session_state.edit_target = None
                        st.rerun()
                else:
                    payload = {"Date": new_date.strftime(DATE_FORMAT), "SPO": new_spo, "Lot ID": new_lot, "Remark": new_rem, "Created At": row.get("Created At", ""), "Updated At": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
                    payload.update(links)
                    if update_record(old_date, new_date.strftime(DATE_FORMAT), row["SPO"], row["Lot ID"], payload):
                        st.success("Updated successfully.")

# --- EXPORT DATA ---
elif choice == "Export Data":
    st.title("📤 Export Data")
    mode = st.radio("Export Scope", ["All Dates", "Specific Date"])
    df_export = pd.DataFrame()
    
    if mode == "Specific Date":
        e_date = st.date_input("Select Date")
        if st.button("Prepare Data"):
            df_export = get_records_by_date(e_date.strftime(DATE_FORMAT))
    else:
        if st.button("Prepare Data"):
            df_export = get_all_records()
            
    if not df_export.empty:
        st.success(f"Prepared {len(df_export)} records.")
        
        csv = df_export.to_csv(index=False).encode('utf-8')
        excel_io = io.BytesIO()
        with pd.ExcelWriter(excel_io, engine='openpyxl') as w:
            df_export.to_excel(w, index=False)
            
        c1, c2 = st.columns(2)
        c1.download_button("Download CSV", data=csv, file_name="export.csv", mime="text/csv", use_container_width=True)
        c2.download_button("Download Excel", data=excel_io.getvalue(), file_name="export.xlsx", use_container_width=True)

# --- SETTINGS ---
elif choice == "Settings":
    st.title("⚙️ Settings")
    st.subheader("System Configurations")
    st.markdown("**Storage Mode:** Cloudinary (Free Image Hosting)")
    st.markdown("**Database Mode:** Single Document Google Sheet, Dynamic Tabs")
    
    st.subheader("Configured Defect Categories")
    for cat in DEFECT_CATEGORIES:
        st.markdown(f"- {cat}")
