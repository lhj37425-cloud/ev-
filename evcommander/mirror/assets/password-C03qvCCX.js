function e(n){return Array.from(n).length>=6&&new TextEncoder().encode(n).length<=72&&!n.includes("\0")}export{e as i};
