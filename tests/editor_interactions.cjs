// Exercise the generated editor's actual JavaScript with a minimal local DOM.
// This is a logic test, not a browser rendering/codec test.
const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
class Element {
 constructor(){this.children=[];this.listeners={};this.dataset={};this.value='';this.checked=false;this.validity='';this.textContent='';}
 append(...items){this.children.push(...items);}
 replaceChildren(...items){this.children=items;}
 addEventListener(name,listener){this.listeners[name]=listener;}
 setCustomValidity(value){this.validity=value;}
 checkValidity(){return !this.validity;}
 reportValidity(){return !this.validity;}
 click(){if(this.onclick)this.onclick();}
}
async function main(){
 const html=fs.readFileSync(process.argv[2],'utf8');
 const payload=html.match(/<script id="data" type="application\/json">([\s\S]*?)<\/script>/)[1];
 const js=[...html.matchAll(/<script>([\s\S]*?)<\/script>/g)].at(-1)[1];
 const storage=new Map(),downloads=[],elements=new Map();
 const get=id=>{if(!elements.has(id))elements.set(id,new Element());return elements.get(id);};
 get('data').textContent=payload;
 const context=vm.createContext({document:{getElementById:get,createElement:()=>new Element(),addEventListener:()=>{}},
  localStorage:{getItem:k=>storage.get(k)??null,setItem:(k,v)=>storage.set(k,v)},
  URL:{createObjectURL:blob=>{downloads.push(blob);return 'blob:test';},revokeObjectURL:()=>{}},
  Blob,setTimeout:fn=>fn(),console});
 vm.runInContext(js,context);
 const originalCount=JSON.parse(payload).corrections.length;
 assert.ok(get('counter').textContent.includes(originalCount+' corrections'));
 get('language').value='en';get('language').listeners.change();
 assert.ok(get('position').textContent.startsWith('EN'));
 const original=get('text').value;
 get('text').value='Test edited caption';get('text').oninput();
 assert.equal(get('text').checkValidity(),true);
 get('download').click();
 let downloaded=JSON.parse(await downloads.at(-1).text());
 assert.ok(downloaded.some(e=>e.text==='Test edited caption'&&e.original===original&&e.language==='en'));
 get('deleted').checked=true;get('deleted').onchange();
 assert.equal(get('text').disabled,true);
 get('download').click();downloaded=JSON.parse(await downloads.at(-1).text());
 assert.ok(downloaded.some(e=>e.delete===true&&e.original===original));
 get('resetCue').click();assert.equal(get('text').value,original);
 get('text').value='';get('text').oninput();assert.equal(get('text').checkValidity(),false);
 const n=downloads.length;get('download').click();assert.equal(downloads.length,n);
 get('text').value=original;get('text').oninput();
 get('search').value='no matching cue 349871203487';get('search').listeners.input();
 assert.equal(get('empty').hidden,false);
 get('search').value='';get('search').listeners.input();
 get('flagged').checked=true;get('flagged').listeners.change();
 assert.ok(get('list').children.every(c=>c.children[0].textContent.includes('Flagged')));
 get('importFile').files=[{text:async()=>JSON.stringify([{language:'en',start:1,original:'MISMATCH',text:'bad'}])}];
 await get('importFile').onchange({target:get('importFile')});
 assert.ok(get('notice').textContent.startsWith('Import failed:'));
 console.log('Editor: language/search/flag filters, editing, deletion, restore, validation and correction download passed.');
}
main().catch(error=>{console.error(error);process.exitCode=1;});
