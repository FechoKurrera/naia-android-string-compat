import importlib.util
import json
from pathlib import Path
import stat
import tempfile
from types import SimpleNamespace as NS
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('tools_entry', ROOT/'main.py')
entry = importlib.util.module_from_spec(spec); spec.loader.exec_module(entry)

class ControllerTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.saved=dict(entry.DEFAULTS);self.writes=[]
        self.record=NS(status='loaded',enabled=True,blocked=False,armed=True)
        self.ctx=NS(_record=self.record,_app_context=NS(runtime_paths=NS(config_dir=Path(self.temp.name))),
                    load_settings=lambda defaults:{**defaults,**self.saved},save_settings=self.save)
        self.opened=[]
        self.c=entry.Controller(self.ctx,entry._private_directory(self.ctx),NS(open_native_folder=lambda p:self.opened.append(p) or {'opened':True}))
        self.addCleanup(self.c.close)
        self.pair={'relay_url':entry.RELAY_URL,'device_id':'device_test','device_token':'PRIVATE-DEVICE',
                   'relay_access_token':'PRIVATE-ACCESS','permissions':{'read':True,'edit':True,'generate':True}}

    def save(self,data):
        self.saved=data.copy();self.writes.append(data.copy());return True

    def test_pairing_readonly_private_file_no_broadcast_secrets(self):
        result=self.c.import_pairing(self.pair)
        self.assertTrue(result['paired']);self.assertFalse(result['settings']['allow_edit'])
        self.assertFalse(result['settings']['allow_generate'])
        self.assertNotIn('PRIVATE-',json.dumps(result)+json.dumps(self.writes))
        self.assertEqual(stat.S_IMODE(self.c.pair_path.stat().st_mode),0o600)
        self.assertEqual(self.c.bridge_settings()['device_token'],'PRIVATE-DEVICE')

    def test_invalid_pairing_origin_type_and_headers_rejected(self):
        for delta in ({'relay_url':None},{'relay_url':'https://evil.example'},
                      {'relay_url':entry.RELAY_URL+'@evil.example'},{'device_token':'x\r\ny'},{'device_id':'../x'}):
            with self.subTest(delta=delta),self.assertRaises(ValueError):self.c.import_pairing({**self.pair,**delta})
        self.assertFalse(self.c.pair_path.exists())

    def test_failed_import_cannot_inherit_enabled_writes(self):
        self.c.import_pairing(self.pair);self.c.update_settings({'allow_edit':True,'allow_generate':True})
        self.ctx.save_settings=lambda s:False if s['plugin_enabled'] else self.save(s)
        with self.assertRaises(ValueError):self.c.import_pairing({**self.pair,'device_token':'NEW'})
        settings=self.c.bridge_settings();self.assertFalse(settings['enabled']);self.assertFalse(settings['permissions']['generate'])
        restarted=entry.Controller(self.ctx,self.c.state_root,self.c.folder_module);self.addCleanup(restarted.close)
        self.assertFalse(restarted.bridge_settings()['enabled']);self.assertFalse(restarted.bridge_settings()['permissions']['edit'])

    def test_permissions_disarm_disconnect_and_close(self):
        self.c.import_pairing(self.pair);self.c.update_settings({'allow_generate':True})
        self.assertTrue(self.c.bridge_settings()['enabled']);self.assertTrue(self.c.bridge_settings()['permissions']['generate'])
        self.record.armed=False;self.assertFalse(self.c.bridge_settings()['enabled']);self.assertFalse(self.c.memory_settings()['low_memory'])
        self.record.armed=True;self.c.disconnect();self.assertFalse(self.c.pair_path.exists())
        self.assertNotIn('device_token',self.c.bridge_settings());self.assertFalse(self.c.bridge_settings()['enabled'])
        closed=[];self.c.bridge=NS(close=lambda:closed.append('bridge'));self.c._cleanup=[lambda:closed.append('ui')]
        self.c.close();self.c.close();self.assertEqual(closed,['bridge','ui'])

    def test_settings_validation_and_memory_toggle(self):
        result=self.c.update_settings({'low_memory':False,'cache_mb':16})
        self.assertFalse(result['settings']['low_memory']);self.assertEqual(result['settings']['cache_mb'],16)
        for delta in ({'cache_mb':True},{'cache_mb':900},{'allow_edit':'true'},{'device_token':'x'},{'plugin_enabled':True}):
            with self.subTest(delta=delta),self.assertRaises(ValueError):self.c.update_settings(delta)

    def test_known_folder_targets_and_disabled_extension(self):
        directory=Path(self.temp.name)/'images'
        self.ctx._app_context._save_service=lambda:NS(current_save_directory=lambda:directory)
        self.assertTrue(self.c.open_folder('save')['opened']);self.assertEqual(self.opened,[directory])
        with self.assertRaises(ValueError):self.c.open_folder('/arbitrary/path')
        self.record.enabled=False
        with self.assertRaises(ValueError):self.c.import_pairing(self.pair)

    def test_history_is_independent_of_plugin_pairing_and_requires_active_extension(self):
        calls=[]
        self.c.history_library=NS(request=lambda a,d:calls.append((a,d)) or {'ok':True},
            asset=lambda i,k:calls.append((i,k)) or (b'image','image/png'),
            source_asset=lambda r,p,k:calls.append((r,p,k)) or (b'thumb','image/webp'),close=lambda:calls.append('closed'))
        self.assertFalse(self.c.bridge_settings()['enabled'])
        self.assertTrue(self.c.history_request('browse',{'relative':''})['ok'])
        self.assertEqual(self.c.history_asset('item','preview'),(b'image','image/png'))
        self.assertEqual(self.c.history_source_asset('output','sample.png','thumb'),(b'thumb','image/webp'))
        self.record.armed=False
        with self.assertRaises(ValueError):self.c.history_request('import',{})
        with self.assertRaises(ValueError):self.c.history_asset('item','image')
        with self.assertRaises(ValueError):self.c.history_source_asset('output','sample.png','thumb')
        self.c.close()
        self.assertEqual(calls,[('browse',{'relative':''}),('item','preview'),('output','sample.png','thumb'),'closed'])

    def test_browser_file_callback_rechecks_active_and_closes_before_history(self):
        events=[]; callbacks=[]
        self.c.history_library=NS(import_stream=lambda *args: events.append('file') or {'imported':1,'duplicates':0},
                                  close=lambda:events.append('history closed'))
        def make_picker(callback):
            callbacks.append(callback)
            return NS(open=lambda:{'opened':True}, close=lambda:events.append('picker closed'))
        self.c._photo_picker_factory=make_picker
        with self.assertRaises(ValueError):self.c.history_request('open_photo_picker',{'path':'/tmp'})
        self.assertFalse(callbacks)
        self.assertTrue(self.c.history_request('open_photo_picker',{})['opened'])
        self.assertEqual(callbacks[0]('photo.png',None,1)['imported'],1)
        self.record.armed=False
        with self.assertRaises(ValueError):callbacks[0]('other.png',None,1)
        self.c.close()
        self.assertEqual(events,['file','picker closed','history closed'])

    def test_comic_server_is_explicit_and_uses_private_token_only_when_enabled(self):
        calls=[]
        self.ctx._app_context.secure_token_manager=NS(get_token=lambda key:calls.append(key) or 'PRIVATE-NAI')
        self.c.prompt_server=NS(prepare=lambda:calls.append('prepare'),stop=lambda:calls.append('stop'),
                               status=lambda:{'state':'starting'},close=lambda:None)
        self.assertEqual(self.c._comic_token(),'')
        self.assertEqual(calls,[])
        result=self.c.prepare_prompt_server()
        self.assertEqual(calls,['prepare'])
        self.assertTrue(result['settings']['comic_server_enabled'])
        self.assertNotIn('PRIVATE',json.dumps(result))
        self.assertEqual(self.c._comic_token(),'PRIVATE-NAI')
        self.assertEqual(calls,['prepare','nai_token'])
        self.record.armed=False
        self.assertEqual(self.c._comic_token(),'')
        with self.assertRaises(ValueError):self.c.prepare_prompt_server()
        self.record.armed=True
        self.c.stop_prompt_server()
        self.assertEqual(self.c._comic_token(),'')
        self.assertFalse(self.saved['comic_server_enabled'])
        with self.assertRaises(ValueError):self.c.update_settings({'comic_server_enabled':True})

    def test_comic_setup_does_not_run_when_preference_cannot_be_saved(self):
        calls=[]
        self.c.prompt_server=NS(prepare=lambda:calls.append('started'),close=lambda:None)
        self.ctx.save_settings=lambda settings:False
        with self.assertRaises(ValueError):self.c.prepare_prompt_server()
        self.assertEqual(calls,[])

    def test_comic_setup_start_failure_rolls_back_enable_preference(self):
        def fail():raise RuntimeError('cannot start worker')
        self.c.prompt_server=NS(prepare=fail,close=lambda:None)
        with self.assertRaises(RuntimeError):self.c.prepare_prompt_server()
        self.assertFalse(self.saved['comic_server_enabled'])

if __name__=='__main__':unittest.main()

