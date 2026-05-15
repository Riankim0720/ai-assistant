import streamlit as st
import anthropic
import httpx
import requests
import json
import re
from requests.auth import HTTPBasicAuth
from prompt import SYSTEM_PROMPT

SEPARATOR = "\n\n---AI 수집 결과---\n"


def get_jira_issue(issue_key):
    url = f"{st.secrets['JIRA_URL']}/rest/api/2/issue/{issue_key}"
    auth = HTTPBasicAuth(st.secrets["JIRA_USERNAME"], st.secrets["JIRA_PASSWORD"])
    headers = {"Accept": "application/json"}
    try:
        response = requests.get(url, auth=auth, headers=headers, timeout=10, verify=False)
        if response.status_code == 200:
            data = response.json()
            summary = data["fields"].get("summary", "")
            description = data["fields"].get("description", "") or ""
            return {"summary": summary, "description": description, "error": None}
        elif response.status_code == 401:
            return {"error": "Jira 인증 실패: 아이디 또는 비밀번호를 확인해주세요."}
        elif response.status_code == 404:
            return {"error": f"이슈를 찾을 수 없습니다: {issue_key}"}
        else:
            return {"error": f"Jira 오류 ({response.status_code})"}
    except requests.exceptions.ConnectionError:
        return {"error": "Jira 서버에 연결할 수 없습니다. URL을 확인해주세요."}
    except Exception as e:
        return {"error": f"오류 발생: {str(e)}"}


def update_jira_description(issue_key, original_description, collected_fields):
    url = f"{st.secrets['JIRA_URL']}/rest/api/2/issue/{issue_key}"
    auth = HTTPBasicAuth(st.secrets["JIRA_USERNAME"], st.secrets["JIRA_PASSWORD"])
    headers = {"Accept": "application/json", "Content-Type": "application/json"}

    ai_result = (
        SEPARATOR
        + "[요건배경] " + collected_fields.get("요건배경", "") + "\n\n"
        + "[AS-IS] " + collected_fields.get("AS_IS", "") + "\n\n"
        + "[TO-BE] " + collected_fields.get("TO_BE", "") + "\n\n"
        + "[발생계기] " + collected_fields.get("발생계기", "")
    )

    sep_marker = SEPARATOR.strip()
    if sep_marker in (original_description or ""):
        base = original_description.split(sep_marker)[0].rstrip()
    else:
        base = (original_description or "").rstrip()

    if base:
        new_description = "[담당자 원문]\n" + base + ai_result
    else:
        new_description = "[담당자 원문]\n(원문 없음)" + ai_result

    payload = {"fields": {"description": new_description}}

    try:
        response = requests.put(
            url, auth=auth, headers=headers,
            data=json.dumps(payload), timeout=10, verify=False
        )
        if response.status_code == 204:
            return True, None
        else:
            return False, "Jira 업데이트 실패 (" + str(response.status_code) + "): " + response.text
    except Exception as e:
        return False, "오류 발생: " + str(e)


def extract_collected_json(text):
    pattern = r"```json\s*(\{.*?\})\s*```"
    matches = re.findall(pattern, text, re.DOTALL)
    for match in matches:
        try:
            data = json.loads(match)
            if data.get("status") == "collected" and "fields" in data:
                return data["fields"]
        except Exception:
            continue
    return None


def build_api_messages(issue_key, issue_data, messages):
    context_msg = (
        "Jira 이슈 [" + issue_key + "]\n"
        "이슈 제목: " + issue_data["summary"] + "\n"
        "담당자 설명: " + (issue_data["description"] or "(없음)")
    )
    api_messages = [{"role": "user", "content": context_msg}]
    for m in messages:
        api_messages.append({"role": m["role"], "content": m["content"]})
    return api_messages


st.set_page_config(page_title="K사 요건 수집 AI", page_icon="🤖", layout="centered")
st.title("🤖 요건 정보 수집 AI 어시스턴트")
st.caption("Jira 이슈 번호를 입력하면 AI가 요건 정보를 수집해 드립니다.")

if "messages" not in st.session_state:
    st.session_state.messages = []
if "issue_loaded" not in st.session_state:
    st.session_state.issue_loaded = False
if "issue_data" not in st.session_state:
    st.session_state.issue_data = None
if "collected" not in st.session_state:
    st.session_state.collected = False
if "current_issue_key" not in st.session_state:
    st.session_state.current_issue_key = ""

with st.form("issue_form"):
    col1, col2 = st.columns([3, 1])
    with col1:
        issue_key_input = st.text_input(
            "Jira 이슈 번호",
            placeholder="예: BIZ-123",
            value=st.session_state.current_issue_key,
        )
    with col2:
        st.write("")
        st.write("")
        submitted = st.form_submit_button("이슈 불러오기")

if submitted and issue_key_input:
    issue_key = issue_key_input.strip().upper()
    with st.spinner("Jira에서 이슈 정보를 불러오는 중..."):
        result = get_jira_issue(issue_key)

    if result.get("error"):
        st.error(result["error"])
    else:
        if issue_key != st.session_state.current_issue_key:
            st.session_state.messages = []
            st.session_state.collected = False

        st.session_state.issue_data = result
        st.session_state.issue_loaded = True
        st.session_state.current_issue_key = issue_key

        if not st.session_state.messages:
            client = anthropic.Anthropic(
                api_key=st.secrets["ANTHROPIC_API_KEY"],
                http_client=httpx.Client(verify=False)
            )
            intro_user_msg = (
                "Jira 이슈 [" + issue_key + "]가 등록되었습니다.\n"
                "이슈 제목: " + result["summary"] + "\n"
                "담당자 설명: " + (result["description"] or "(없음)") + "\n\n"
                "위 이슈에 대해 요건 정보 수집을 시작해 주세요."
            )
            with st.spinner("AI가 첫 인사말을 작성 중..."):
                ai_response = client.messages.create(
                    model="claude-opus-4-7",
                    max_tokens=1024,
                    system=SYSTEM_PROMPT,
                    messages=[{"role": "user", "content": intro_user_msg}],
                )
            ai_text = ai_response.content[0].text
            st.session_state.messages.append({"role": "assistant", "content": ai_text})

if st.session_state.issue_loaded and st.session_state.issue_data:
    with st.expander("📋 " + st.session_state.current_issue_key + " 이슈 정보", expanded=False):
        st.write("**제목:** " + st.session_state.issue_data["summary"])

if st.session_state.collected:
    st.success("✅ 요건 정보 수집 완료! Jira Description이 업데이트되었습니다.")

for msg in st.session_state.messages:
    if msg["role"] == "assistant":
        display_text = re.sub(r"```json.*?```", "", msg["content"], flags=re.DOTALL).strip()
        with st.chat_message("assistant", avatar="🤖"):
            st.markdown(display_text)
    else:
        with st.chat_message("user", avatar="🙋"):
            st.markdown(msg["content"])

if st.session_state.issue_loaded and not st.session_state.collected:
    user_input = st.chat_input("답변을 입력해 주세요...")

    if user_input:
        st.session_state.messages.append({"role": "user", "content": user_input})

        with st.chat_message("user", avatar="🙋"):
            st.markdown(user_input)

        with st.chat_message("assistant", avatar="🤖"):
            with st.spinner("AI가 응답 중..."):
                client = anthropic.Anthropic(
                    api_key=st.secrets["ANTHROPIC_API_KEY"],
                    http_client=httpx.Client(verify=False)
                )
                api_messages = build_api_messages(
                    st.session_state.current_issue_key,
                    st.session_state.issue_data,
                    st.session_state.messages,
                )
                ai_response = client.messages.create(
                    model="claude-opus-4-7",
                    max_tokens=2048,
                    system=SYSTEM_PROMPT,
                    messages=api_messages,
                )
                ai_text = ai_response.content[0].text

            display_text = re.sub(r"```json.*?```", "", ai_text, flags=re.DOTALL).strip()
            st.markdown(display_text)

        st.session_state.messages.append({"role": "assistant", "content": ai_text})

        collected_fields = extract_collected_json(ai_text)
        if collected_fields:
            with st.spinner("Jira Description 업데이트 중..."):
                success, err_msg = update_jira_description(
                    st.session_state.current_issue_key,
                    st.session_state.issue_data["description"],
                    collected_fields,
                )
            if success:
                st.session_state.collected = True
                st.rerun()
            else:
                st.error("Jira 업데이트 실패: " + err_msg)

elif st.session_state.issue_loaded and st.session_state.collected:
    if st.button("새 이슈 수집 시작"):
        st.session_state.messages = []
        st.session_state.issue_loaded = False
        st.session_state.issue_data = None
        st.session_state.collected = False
        st.session_state.current_issue_key = ""
        st.rerun()
else:
    st.info("위에서 Jira 이슈 번호를 입력하고 '이슈 불러오기'를 눌러주세요.")
