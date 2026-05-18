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
# 3. GOOGLE SHEETS CLIENT & DYNAMIC CONFIG
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

def open_spreadsheet():
    client = get_sheets_client()
    return client.open_by_key(st.secrets["GOOGLE_SHEET_ID"])

def get_dynamic_categories():
    doc = open_spreadsheet()
    try:
        ws = doc.worksheet("System_Config")
        records = ws.get_all_records()
        cats = [str(r["Defect Categories"]).strip() for r in records if str(r.get("Defect Categories", "")).strip()]
        if not cats: return ["Flashes", "Chip", "Lead", "Scratches", "Expose Copper", "Contam"]
        return cats
    except gspread.exceptions.WorksheetNotFound:
        # Create config sheet if it doesn't exist
        ws = doc.add_worksheet(title="System_Config", rows="100", cols="5")
        default_cats = ["Flashes", "Chip", "Lead", "Scratches", "Expose Copper", "Contam"]
        ws.update("A1:A7", [["Defect Categories"]] + [[c] for c in default_cats])
        return default_cats

def save_dynamic_categories(new_categories):
    doc = open_spreadsheet()
    ws = doc.worksheet("System_Config")
    ws.clear()
    ws.update(f"A1:A{len(new_categories)+1}", [["Defect Categories"]] + [[c] for c in new_categories])

def get_expected_headers():
    return ["Date", "Product", "SPO", "Lot ID"] + DEFECT_CATEGORIES + ["Remark", "Created At", "Updated At"]

# Load Categories globally for this session
DEFECT_CATEGORIES = get_dynamic_categories()

# ==========================================
# 4. CLOUDINARY (IMAGE STORAGE)
# ==========================================
def upload_image_to_cloudinary(image_bytes, filename):
    try:
        base64_image = "data:image/jpeg;base64," + base64.b64encode(image_bytes).decode('utf-8')
        response = cloudinary.uploader.upload(base64_image, public_id=filename.split('.')[0], folder="SPO_Defects")
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
# 5. GOOGLE SHEETS (DATABASE CRUD)
# ==========================================
def ensure_headers(worksheet):
    headers = get_expected_headers()
    existing = worksheet.row_values(1)
    if not existing:
        worksheet.insert_row(headers, 1)
        worksheet.format("A1:Z1", {"textFormat": {"bold": True}})
        worksheet.freeze(rows=1)
    elif len(existing) < len(headers):
        # Update headers if new categories were added
        worksheet.update("A1:Z1", [headers])

def sync_all_headers():
    doc = open_spreadsheet()
    for ws in doc.worksheets():
        if re.match(r'^\d{4}-\d{2}-\d{2}$', ws.title):
            ensure_headers(ws)

def get_or_create_date_sheet(date_str):
    doc = open_spreadsheet()
    try:
        ws = doc.worksheet(date_str)
    except gspread.exceptions.WorksheetNotFound:
        ws = doc.add_worksheet(title=date_str, rows="1000", cols="30")
    ensure_headers(ws)
    return ws

def append_or_update_record(date_str, product, spo, lot_id, defect_links, remark):
    ws = get_or_create_date_sheet(date_str)
    records = ws.get_all_records(value_render_option='FORMULA')
    df = pd.DataFrame(records)
    
    now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    row_map = {"Date": date_str, "Product": product, "SPO": str(spo), "Lot ID": str(lot_id), "Remark": remark, "Updated At": now_str}
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

    # Inject image formulas
    for cat, link in defect_links.items():
        if link:
            formula = f'=IMAGE("{link}")'
            # If replacing an existing one, or appending
            row_map[cat] = formula
        elif link == "":
            # Clear it if user deleted it
            row_map[cat] = ""

    headers = get_expected_headers()
    ordered_row = [row_map.get(h, "") for h in headers]
    
    if row_index != -1:
        ws.update(range_name=f"A{row_index}", values=[ordered_row], value_input_option='USER_ENTERED')
    else:
        ws.append_row(ordered_row, value_input_option='USER_ENTERED')
    return True

def clean_formula_df(df):
    if df.empty: return df
    for col in df.columns:
        df[col] = df[col].astype(str).apply(
            lambda x: re.sub(r'=IMAGE\("([^"]+)"\)', r'\1', x) if pd.notna(x) and 'IMAGE' in x else (x if x != 'None' else "")
        )
    return df

def get_records_by_date(date_str):
    doc = open_spreadsheet()
    try:
        data = doc.worksheet(date_str).get_all_records(value_render_option='FORMULA')
        return clean_formula_df(pd.DataFrame(data))
    except gspread.exceptions.WorksheetNotFound:
        return pd.DataFrame()

def get_all_records():
    doc = open_spreadsheet()
    all_dfs = []
    for ws in doc.worksheets():
        if re.match(r'^\d{4}-\d{2}-\d{2}$', ws.title):
            data = ws.get_all_records(value_render_option='FORMULA')
            if data:
                all_dfs.append(pd.DataFrame(data))
    return clean_formula_df(pd.concat(all_dfs, ignore_index=True)) if all_dfs else pd.DataFrame()

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

# ==========================================
# 6. HTML GRID GENERATOR (MIMICS EXCEL)
# ==========================================
def render_excel_like_table(df):
    if df.empty:
        st.info("No records match your filters.")
        return

    # Define the exact HTML structure to look like the screenshot
    html = """
    <style>
    .excel-table { border-collapse: collapse; width: 100%; font-family: sans-serif; font-size: 14px; }
    .excel-table th { background-color: #f2f2f2; border: 2px solid #000; padding: 8px; text-align: center; }
    .excel-table td { border: 1px solid #000; padding: 5px; text-align: center; vertical-align: middle; height: 100px;}
    .img-cell { width: 100px; height: 100px; object-fit: contain; }
    </style>
    <table class="excel-table">
        <thead>
            <tr>
                <th>Product</th>
                <th>SPO</th>
                <th>Lot ID</th>
    """
    for cat in DEFECT_CATEGORIES:
        html += f"<th>{cat}</th>"
    html += "<th>Remark</th></tr></thead><tbody>"

    for _, row in df.iterrows():
        html += f"""
            <tr>
                <td>{row.get('Product', '')}</td>
                <td>{row.get('SPO', '')}</td>
                <td>{row.get('Lot ID', '')}</td>
        """
        for cat in DEFECT_CATEGORIES:
            img_url = row.get(cat, "")
            if "http" in str(img_url):
                # Clean up if multiple exist
                first_url = str(img_url).split(",")[0].strip()
                html += f'<td><img src="{first_url}" class="img-cell" onclick="window.open(\'{first_url}\', \'_blank\')"></td>'
            else:
                html += "<td></td>"
        
        html += f"<td>{row.get('Remark', '')}</td></tr>"
    
    html += "</tbody></table>"
    st.markdown(html, unsafe_allow_html=True)


# ==========================================
# 7. UI ROUTING & INTERFACE
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

# --- DASHBOARD ---
if choice == "Dashboard":
    st.title("📊 Dashboard")
    with st.spinner("Loading records..."):
        df = get_all_records()
    
    if df.empty:
        st.info("No data available.")
    else:
        total_lots = len(df)
        defect_counts = {c: df[c].apply(lambda x: 1 if pd.notna(x) and 'http' in str(x) else 0).sum() for c in DEFECT_CATEGORIES if c in df.columns}
        
        c1, c2, c3 = st.columns(3)
        c1.metric("Total Lots Inspected", total_lots)
        c2.metric("Total Defects", sum(defect_counts.values()))
        c3.metric("Defects per Lot", round(sum(defect_counts.values()) / total_lots, 2) if total_lots > 0 else 0)
        
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
        c1, c2, c3, c4 = st.columns(4)
        date_val = c1.date_input("Date", datetime.date.today())
        product_val = c2.text_input("Product (e.g. LFPAK)").strip()
        spo_val = c3.text_input("SPO").strip()
        lot_val = c4.text_input("Lot ID").strip()
        
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
            if not spo_val or not lot_val or not product_val:
                st.error("Product, SPO, and Lot ID are required.")
            elif selected_defects and len(images_map) != len(selected_defects):
                st.error("An image must be provided for EVERY selected defect category.")
            else:
                with st.spinner("Uploading images & saving to Google Sheets..."):
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
                        success = append_or_update_record(date_val.strftime(DATE_FORMAT), product_val, spo_val, lot_val, uploaded_links, remark_val)
                        if success:
                            st.success("Record saved successfully! Images will render in Google Sheets.")
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

    with st.spinner("Fetching Data..."):
        df = get_records_by_date(f_date.strftime(DATE_FORMAT)) if f_date else get_all_records()
    
    if not df.empty:
        if f_spo: df = df[df["SPO"].astype(str).str.contains(f_spo, case=False)]
        if f_lot: df = df[df["Lot ID"].astype(str).str.contains(f_lot, case=False)]
        render_excel_like_table(df)
    else:
        st.info("No records found.")

# --- DAILY INSPECTION VIEW ---
elif choice == "Daily Inspection View":
    st.title("📅 Daily View")
    target_date = st.date_input("Select Date", datetime.date.today())
    with st.spinner("Fetching Data..."):
        df = get_records_by_date(target_date.strftime(DATE_FORMAT))
    
    if not df.empty:
        st.subheader(f"Summary for {target_date.strftime(DATE_FORMAT)}")
        render_excel_like_table(df)
    else:
        st.info("No records for this date.")

# --- EDIT RECORDS ---
elif choice == "Edit Records":
    st.title("🛠️ Edit & Manage Records")
    st.markdown("Search a record to edit text, delete images, or replace images.")
    
    c1, c2, c3 = st.columns(3)
    s_date = c1.date_input("Record Date", datetime.date.today())
    s_spo = c2.text_input("Record SPO").strip()
    s_lot = c3.text_input("Record Lot ID").strip()
    
    if st.button("Search Record"):
        with st.spinner("Searching..."):
            df = get_records_by_date(s_date.strftime(DATE_FORMAT))
            if not df.empty:
                match = df[(df["SPO"].astype(str) == str(s_spo)) & (df["Lot ID"].astype(str) == str(s_lot))]
                if not match.empty:
                    st.session_state.edit_target = match.iloc[0].to_dict()
                    st.success("Record found.")
                else:
                    st.session_state.edit_target = None
                    st.error("Record not found.")
            else:
                st.session_state.edit_target = None
                st.error("Record not found.")
                
    if "edit_target" in st.session_state and st.session_state.edit_target:
        row = st.session_state.edit_target
        
        with st.form("edit_form"):
            st.subheader("Text Details")
            c_ed1, c_ed2, c_ed3 = st.columns(3)
            new_product = c_ed1.text_input("Product", row.get("Product", ""))
            new_spo = c_ed2.text_input("SPO", row["SPO"])
            new_lot = c_ed3.text_input("Lot ID", row["Lot ID"])
            new_rem = st.text_area("Remark", row.get("Remark", ""))
            
            st.markdown("---")
            st.subheader("Manage Images")
            
            # Create a visual grid for editing images
            image_updates = {}
            delete_flags = {}
            
            for cat in DEFECT_CATEGORIES:
                st.markdown(f"**{cat}**")
                col_img, col_act = st.columns([1, 2])
                
                current_link = str(row.get(cat, ""))
                
                with col_img:
                    if "http" in current_link:
                        st.image(current_link.split(",")[0], width=150)
                    else:
                        st.info("No image.")
                        
                with col_act:
                    if "http" in current_link:
                        delete_flags[cat] = st.checkbox(f"🗑️ Delete {cat} Image", key=f"del_{cat}")
                    image_updates[cat] = st.file_uploader(f"Replace/Add {cat} Image", type=["jpg", "png", "jpeg"], key=f"repl_{cat}")
                
                st.markdown("---")

            action = st.radio("Execute Action", ["Update Record", "Delete Entire Record"])
            
            if st.form_submit_button("Save Changes"):
                old_date = s_date.strftime(DATE_FORMAT)
                if action == "Delete Entire Record":
                    if delete_record(old_date, row["SPO"], row["Lot ID"]):
                        st.success("Record deleted entirely.")
                        st.session_state.edit_target = None
                        st.rerun()
                else:
                    with st.spinner("Processing Updates..."):
                        # Build the defect links payload
                        final_links = {}
                        error_flag = False
                        
                        for cat in DEFECT_CATEGORIES:
                            current_url = str(row.get(cat, ""))
                            
                            # 1. Did they upload a new image?
                            if image_updates[cat] is not None:
                                img_bytes, filename = process_image(image_updates[cat], new_spo, new_lot, cat)
                                new_url = upload_image_to_cloudinary(img_bytes, filename)
                                if new_url:
                                    final_links[cat] = new_url
                                else:
                                    error_flag = True
                            
                            # 2. Did they check the delete box?
                            elif cat in delete_flags and delete_flags[cat]:
                                final_links[cat] = "" # Clear the cell
                                
                            # 3. Otherwise, keep existing
                            else:
                                final_links[cat] = current_url
                        
                        if not error_flag:
                            # Save to Google Sheets
                            append_or_update_record(old_date, new_product, new_spo, new_lot, final_links, new_rem)
                            st.success("Record updated successfully!")
                            st.session_state.edit_target = None

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
    st.title("⚙️ System Settings")
    st.subheader("Manage Defect Categories")
    st.markdown("Categories listed here will automatically appear as columns in your Google Sheets and Dashboard.")
    
    current_cats = get_dynamic_categories()
    
    # Display current categories
    st.write("Current Active Categories:")
    for cat in current_cats:
        st.markdown(f"- **{cat}**")
        
    st.markdown("---")
    
    with st.form("category_form"):
        new_cat = st.text_input("Add New Defect Category")
        
        # Multiselect to remove
        cats_to_remove = st.multiselect("Remove Categories", current_cats)
        
        if st.form_submit_button("Update Categories"):
            updated_cats = current_cats.copy()
            
            if new_cat and new_cat not in updated_cats:
                updated_cats.append(new_cat.strip())
            
            for c in cats_to_remove:
                if c in updated_cats:
                    updated_cats.remove(c)
                    
            if len(updated_cats) == 0:
                st.error("You must have at least one defect category.")
            else:
                with st.spinner("Saving categories and syncing database headers..."):
                    save_dynamic_categories(updated_cats)
                    # Global variable update
                    DEFECT_CATEGORIES = updated_cats 
                    sync_all_headers()
                    st.success("Categories updated! All Google Sheet tabs have been synced.")
                    st.rerun()
