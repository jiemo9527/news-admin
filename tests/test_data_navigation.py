from bs4 import BeautifulSoup
from tests.test_web import make_client, make_viewer, seed_run


def test_data_navigation_has_icon_and_keeps_existing_layout(tmp_path):
    _, client = make_client(tmp_path)
    soup=BeautifulSoup(client.get('/admin/data').text,'html.parser')
    link=soup.select_one('.sidenav a[href="/admin/data"]')
    assert link and link.select_one('svg[aria-hidden="true"]')
    viewer=make_viewer(client)
    assert '/admin/data' not in viewer.get('/admin/settings').text


def test_clear_restore_have_no_confirmation_prompt_but_require_password(tmp_path):
    rt,client=make_client(tmp_path)
    rid=seed_run(rt)
    soup=BeautifulSoup(client.get('/admin/data').text,'html.parser')
    for action in ('clear','restore'):
        form=soup.select_one(f'form[action="/admin/data/{action}"]')
        assert form.select_one('[name=confirm]') is None
        assert not form.get('onsubmit')
        assert form.select_one('[name=password][required]')
    backup=client.post('/admin/data/export',data={'password':'pw'})
    bad=client.post('/admin/data/clear',data={'password':'wrong'},follow_redirects=False)
    assert bad.status_code==400 and rt.store.get_run(rid)
    response=client.post('/admin/data/clear',data={'password':'pw'},follow_redirects=False)
    assert response.status_code==303 and rt.store.get_run(rid) is None
    restored=client.post('/admin/data/restore',data={'password':'pw'},files={'file':('backup.zip',backup.content,'application/zip')},follow_redirects=False)
    assert restored.status_code==303 and rt.store.get_run(rid)
